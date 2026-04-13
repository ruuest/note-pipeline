"""Note自動投稿ツール — Webダッシュボード (FastAPI + Jinja2)"""
from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

# プロジェクトルートを sys.path に追加して既存 src をインポート可能にする
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from src.generator import (
    generate_article,
    get_unused_keywords,
    load_keywords,
    load_templates,
    mark_keyword_used,
    save_draft,
    load_draft,
)
from src.publisher import publish_article
from src.scheduler import (
    MAX_DAILY_POSTS,
    can_post,
    get_status,
    get_todays_post_count,
    log_post,
    _load_log,
)
from src.validator import validate_article

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Note Pipeline Dashboard")

# Python 3.14 + Jinja2 3.1.x LRUCache workaround: create env with cache disabled
import jinja2 as _jinja2

_jinja2_env = _jinja2.Environment(
    loader=_jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=True,
    auto_reload=True,
    cache_size=0,
)
templates = Jinja2Templates(env=_jinja2_env)

DRAFTS_DIR = PROJECT_ROOT / "drafts"
LOGS_DIR = PROJECT_ROOT / "logs"

# ---------------------------------------------------------------------------
# Basic Auth
# ---------------------------------------------------------------------------
security = HTTPBasic()

_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    if not _AUTH_USER or not _AUTH_PASS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="認証情報が未設定です (BASIC_AUTH_USER / BASIC_AUTH_PASS)",
        )
    user_ok = secrets.compare_digest(credentials.username.encode(), _AUTH_USER.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), _AUTH_PASS.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証失敗",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _draft_files() -> list[Path]:
    """有効な下書き JSON ファイル一覧（古い順）"""
    if not DRAFTS_DIR.exists():
        return []
    return sorted(DRAFTS_DIR.glob("*.json"))


def _keyword_stats() -> dict:
    keywords = load_keywords()
    total = len(keywords)
    used = sum(1 for k in keywords if k["used"].strip().lower() == "true")
    return {"total": total, "used": used, "unused": total - used}


def _load_history(days: int = 7) -> list[dict]:
    """過去 N 日分の投稿ログを返す（新しい順）"""
    history = []
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        entries = _load_log(d)
        for e in entries:
            e["_date"] = d.isoformat()
        history.extend(entries)
    return history


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

_bg_status: dict = {"generating": False, "publishing": False, "last_error": None}


def _bg_generate():
    """バックグラウンドで記事1本生成"""
    _bg_status["generating"] = True
    _bg_status["last_error"] = None
    try:
        keywords = get_unused_keywords(1)
        if not keywords:
            _bg_status["last_error"] = "未使用キーワードがありません"
            return
        kw = keywords[0]
        templates_data = load_templates()
        article = generate_article(kw, templates_data)
        save_draft(article)
        mark_keyword_used(kw["theme"])
    except Exception as e:
        _bg_status["last_error"] = str(e)
    finally:
        _bg_status["generating"] = False


def _bg_publish(draft_path: str):
    """バックグラウンドで下書き1本投稿"""
    _bg_status["publishing"] = True
    _bg_status["last_error"] = None
    try:
        fp = Path(draft_path)
        if not fp.exists():
            _bg_status["last_error"] = "下書きファイルが見つかりません"
            return
        article = load_draft(fp)
        validation = validate_article(article)
        if not validation.is_valid:
            _bg_status["last_error"] = f"品質ゲートNG: {validation.format()}"
            invalid_path = fp.with_suffix(".invalid.json")
            fp.rename(invalid_path)
            return
        result = publish_article(article)
        log_post(result)
        if result.success:
            fp.unlink()
        else:
            _bg_status["last_error"] = f"投稿失敗: {result.error}"
            failed_path = fp.with_suffix(".failed.json")
            fp.rename(failed_path)
    except Exception as e:
        _bg_status["last_error"] = str(e)
    finally:
        _bg_status["publishing"] = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, _=Depends(verify_credentials)):
    sched_status = get_status()
    kw_stats = _keyword_stats()
    drafts = _draft_files()
    return templates.TemplateResponse(request, "index.html", {
        "status": sched_status,
        "kw_stats": kw_stats,
        "draft_count": len(drafts),
        "max_daily": MAX_DAILY_POSTS,
        "bg_status": _bg_status,
    })


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, _=Depends(verify_credentials)):
    entries = _load_history(7)
    return templates.TemplateResponse(request, "history.html", {
        "entries": entries,
    })


@app.get("/drafts", response_class=HTMLResponse)
def drafts_list(request: Request, _=Depends(verify_credentials)):
    drafts = _draft_files()
    draft_articles = []
    for fp in drafts:
        try:
            article = load_draft(fp)
            draft_articles.append({
                "filename": fp.name,
                "filepath": str(fp),
                "title": article.title,
                "keyword": article.keyword,
                "category": article.category,
                "template_id": article.template_id,
                "generated_at": article.generated_at.strftime("%Y-%m-%d %H:%M"),
                "body_len": len(article.body),
            })
        except Exception:
            draft_articles.append({
                "filename": fp.name,
                "filepath": str(fp),
                "title": "(読み込みエラー)",
                "keyword": "",
                "category": "",
                "template_id": "",
                "generated_at": "",
                "body_len": 0,
            })
    return templates.TemplateResponse(request, "drafts.html", {
        "drafts": draft_articles,
        "bg_status": _bg_status,
    })


@app.get("/keywords", response_class=HTMLResponse)
def keywords_list(request: Request, _=Depends(verify_credentials)):
    keywords = load_keywords()
    return templates.TemplateResponse(request, "keywords.html", {
        "keywords": keywords,
        "stats": _keyword_stats(),
    })


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _=Depends(verify_credentials)):
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    google_key_status = "設定済み" if google_key else "未設定"
    return templates.TemplateResponse(request, "settings.html", {
        "google_key_status": google_key_status,
        "max_daily_posts": MAX_DAILY_POSTS,
        "basic_auth_user": _AUTH_USER or "(未設定)",
    })


@app.post("/generate")
def generate_one(background_tasks: BackgroundTasks, _=Depends(verify_credentials)):
    if _bg_status["generating"]:
        raise HTTPException(status_code=409, detail="生成処理が実行中です")
    background_tasks.add_task(_bg_generate)
    return RedirectResponse(url="/drafts", status_code=303)


@app.post("/publish")
def publish_one(
    request: Request,
    background_tasks: BackgroundTasks,
    _=Depends(verify_credentials),
):
    if _bg_status["publishing"]:
        raise HTTPException(status_code=409, detail="投稿処理が実行中です")
    if not can_post():
        raise HTTPException(status_code=429, detail="投稿上限または間隔制限中")

    drafts = _draft_files()
    if not drafts:
        raise HTTPException(status_code=404, detail="下書きがありません")

    draft_path = str(drafts[0])
    background_tasks.add_task(_bg_publish, draft_path)
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# API endpoints (JSON)
# ---------------------------------------------------------------------------

@app.get("/api/status")
def api_status(_=Depends(verify_credentials)):
    return {
        "scheduler": get_status(),
        "keywords": _keyword_stats(),
        "draft_count": len(_draft_files()),
        "bg": _bg_status,
    }
