"""X(Twitter) スレッド自動投稿。

note記事を Claude Sonnet 4.6 で 3〜7 ツイートのスレッドに分割し、
tweepy 経由で X API v2 (OAuth1.0a User Context) で順次投稿する。

【プロダクト大臣の仕様 v1.0 (2026-04-20) 準拠】
- システムプロンプト: docs/x_thread_prompt.md の §「システムプロンプト」を抽出
- 入力スキーマ: topic / axis (A|B|C) / thread_length (3-7) / source_article_url
              / source_snippets[] / hashtags[] / cta_variant (free_trial|doc_dl|inquiry)
- 出力スキーマ: [{index, text, has_link, char_count}]  JSONのみ
- バリデーション: config/compliance_rules.yaml の CR001-CR050 を全件スキャン
- 再生成: 違反検出時は最大3回まで再生成、それでもNGなら承認キューへ
- 制約: 各 text 135字以内 / 3〜7本 / 絵文字1ツイート2個以内 / CTA末尾1箇所のみ
- LP URL: https://nvcloud-lp.pages.dev/ のみ使用
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from src.models import Article

BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "queue"
QUEUE_FILE = QUEUE_DIR / "x_posts.json"
APPROVAL_QUEUE_FILE = QUEUE_DIR / "x_approval_queue.json"  # 3回再生成失敗の承認待ち
PROMPT_PATH = BASE_DIR / "docs" / "x_thread_prompt.md"
TONE_GUIDE_PATH = BASE_DIR / "docs" / "x_tone_guide.md"
COMPLIANCE_RULES_PATH = BASE_DIR / "config" / "compliance_rules.yaml"
TELEGRAM_NOTIFY = Path("/Users/apple/NorthValueAsset/cabinet/scripts/telegram_notify.sh")

# X API v2 制約
TWEET_HARD_LIMIT = 280
TWEET_SAFE_LIMIT = 135
THREAD_MIN = 3
THREAD_MAX = 7
EMOJI_PER_TWEET_MAX = 2
DEFAULT_CTA_URL = "https://nvcloud-lp.pages.dev/"
ALLOWED_URL = "https://nvcloud-lp.pages.dev/"

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_REGEN = 3

# CTA バリエーション
CTA_VARIANTS = {
    "free_trial": f"機能詳細+無料体験はこちら → {ALLOWED_URL}",
    "doc_dl": f"資料ダウンロードはLPから → {ALLOWED_URL}",
    "inquiry": f"導入相談はLPのお問い合わせフォームから → {ALLOWED_URL}",
}


# ─────────────────────────────────────────────────────
# 設定 / 認証
# ─────────────────────────────────────────────────────
@dataclass
class XPublisherConfig:
    api_key: str
    api_secret: str
    access_token: str
    access_token_secret: str
    enabled: bool = False
    anthropic_api_key: str = ""

    @classmethod
    def from_env(cls) -> "XPublisherConfig":
        return cls(
            api_key=os.environ.get("X_API_KEY", ""),
            api_secret=os.environ.get("X_API_SECRET", ""),
            access_token=os.environ.get("X_ACCESS_TOKEN", ""),
            access_token_secret=os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
            enabled=os.environ.get("X_SHARE_ENABLED", "false").lower() == "true",
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )

    def has_x_credentials(self) -> bool:
        return all([self.api_key, self.api_secret, self.access_token, self.access_token_secret])

    def is_ready(self) -> bool:
        return self.enabled and self.has_x_credentials()


# ─────────────────────────────────────────────────────
# システムプロンプト読み込み
# ─────────────────────────────────────────────────────
def load_system_prompt() -> str:
    """docs/x_thread_prompt.md から ```` で囲まれたシステムプロンプトを抽出。"""
    if not PROMPT_PATH.exists():
        # フォールバック: 最低限のプロンプト
        return (
            "あなたはNV CLOUDの公式X投稿スレッドを生成するコピーライターです。"
            "JSON配列のみで返却してください。"
        )
    text = PROMPT_PATH.read_text(encoding="utf-8")
    # 4連バッククォートで囲まれたブロックを抽出（プロンプト本体）
    match = re.search(r"````\s*\n(.*?)\n````", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text  # フォールバック: 全体を返す


# ─────────────────────────────────────────────────────
# コンプライアンスルール (compliance_rules.yaml)
# ─────────────────────────────────────────────────────
_RULES_CACHE: list[dict] | None = None


def load_compliance_rules() -> list[dict]:
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE
    if not COMPLIANCE_RULES_PATH.exists():
        _RULES_CACHE = []
        return _RULES_CACHE
    import yaml

    with open(COMPLIANCE_RULES_PATH, encoding="utf-8") as f:
        rules = yaml.safe_load(f) or []
    _RULES_CACHE = [r for r in rules if isinstance(r, dict) and "id" in r]
    return _RULES_CACHE


# ─────────────────────────────────────────────────────
# 絵文字カウント
# ─────────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)


def count_emoji(text: str) -> int:
    return sum(len(m) for m in _EMOJI_RE.findall(text))


# ─────────────────────────────────────────────────────
# バリデーション
# ─────────────────────────────────────────────────────
@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _strip_url(text: str) -> str:
    return re.sub(r"https?://\S+", "", text)


def _check_compliance(text: str, rules: list[dict]) -> tuple[list[str], list[str]]:
    """compliance ルールにテキストを照合。block→errors / warn→warnings。"""
    errors: list[str] = []
    warnings: list[str] = []
    for rule in rules:
        pattern = rule.get("ng_pattern")
        if not pattern:
            continue
        try:
            if rule.get("regex"):
                hit = re.search(pattern, text)
            else:
                hit = pattern in text
        except re.error:
            continue
        if not hit:
            continue
        msg = f"{rule['id']} ({rule.get('category','?')}/{rule.get('severity','?')}): {rule.get('reason','')}"
        if rule.get("severity") == "block":
            errors.append(msg)
        else:
            warnings.append(msg)
    return errors, warnings


def validate_thread(thread: list[dict]) -> ValidationResult:
    """生成されたスレッドを規約・体裁の両面で検証。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not (THREAD_MIN <= len(thread) <= THREAD_MAX):
        errors.append(
            f"スレッド本数 {len(thread)} が範囲外（許容 {THREAD_MIN}〜{THREAD_MAX}）"
        )

    rules = load_compliance_rules()

    for i, t in enumerate(thread):
        if not isinstance(t, dict) or "text" not in t:
            errors.append(f"index={i+1}: dict形式でない/textフィールド欠落")
            continue
        text = str(t["text"])
        idx = t.get("index", i + 1)
        body_no_url = _strip_url(text)

        # 本文文字数（URL除く）
        if len(body_no_url) > TWEET_SAFE_LIMIT:
            errors.append(
                f"index={idx}: 本文 {len(body_no_url)}字、上限{TWEET_SAFE_LIMIT}字超過"
            )
        # 全体文字数（URL含む）— X 上限
        if len(text) > TWEET_HARD_LIMIT:
            errors.append(
                f"index={idx}: 全体 {len(text)}字、X上限{TWEET_HARD_LIMIT}字超過"
            )
        # 絵文字
        emj = count_emoji(text)
        if emj > EMOJI_PER_TWEET_MAX:
            errors.append(f"index={idx}: 絵文字 {emj}個、上限{EMOJI_PER_TWEET_MAX}個超過")
        # コンプライアンス
        e, w = _check_compliance(text, rules)
        for m in e:
            errors.append(f"index={idx}: {m}")
        for m in w:
            warnings.append(f"index={idx}: {m}")

    # CTA: 末尾1本のみ has_link=true、URLは ALLOWED_URL のみ
    cta_indices = [i for i, t in enumerate(thread) if t.get("has_link")]
    if len(cta_indices) != 1:
        errors.append(f"CTAは末尾1箇所のみ。検出: {len(cta_indices)}箇所")
    elif cta_indices[0] != len(thread) - 1:
        errors.append(f"CTAが末尾でない（index={cta_indices[0]+1}）")

    for i, t in enumerate(thread):
        urls = re.findall(r"https?://\S+", str(t.get("text", "")))
        for u in urls:
            normalized = u.rstrip("。、．，)）」]」")
            if normalized != ALLOWED_URL:
                errors.append(
                    f"index={i+1}: 許可URL以外を含む（{normalized}）。許可: {ALLOWED_URL}"
                )

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)


# ─────────────────────────────────────────────────────
# Claude による生成
# ─────────────────────────────────────────────────────
def _build_user_prompt(input_data: dict) -> str:
    snippets = input_data.get("source_snippets") or []
    snippets_block = "\n".join(f"- {s}" for s in snippets) if snippets else "（なし）"
    hashtags = input_data.get("hashtags") or ["#買取業界", "#出張買取"]
    cta_variant = input_data.get("cta_variant", "free_trial")
    return (
        f"topic: {input_data.get('topic','')}\n"
        f"axis: {input_data.get('axis','A')}\n"
        f"thread_length: {input_data.get('thread_length', 5)}\n"
        f"source_article_url: {input_data.get('source_article_url') or 'なし'}\n"
        f"source_snippets:\n{snippets_block}\n"
        f"hashtags: {' '.join(hashtags)}\n"
        f"cta_variant: {cta_variant}\n"
    )


def _parse_thread_json(raw: str) -> list[dict]:
    """Claude応答からJSON配列を抽出して dict のリストに。"""
    cleaned = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    else:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError(f"JSON配列ではない: {type(parsed)}")
    # char_count を text の長さで上書き（Claudeの計算ミスを補正）
    normalized = []
    for i, item in enumerate(parsed):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise ValueError(f"item#{i}が不正: {item!r}")
        text = str(item.get("text", ""))
        normalized.append({
            "index": int(item.get("index", i + 1)),
            "text": text,
            "has_link": bool(item.get("has_link", False)),
            "char_count": len(_strip_url(text)),
        })
    return normalized


def _call_claude(input_data: dict, *, api_key: str, model: str = CLAUDE_MODEL) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=load_system_prompt(),
        messages=[{"role": "user", "content": _build_user_prompt(input_data)}],
    )
    raw = "".join(b.text for b in response.content if hasattr(b, "text"))
    return _parse_thread_json(raw)


def generate_thread(
    input_data: dict,
    *,
    api_key: str | None = None,
    model: str = CLAUDE_MODEL,
    max_regen: int = MAX_REGEN,
) -> dict:
    """スレッド生成 + バリデーション + 最大3回再生成。

    Returns:
        {
          "ok": bool,            # 全バリデーション通過
          "thread": [...],       # 最終出力 (失敗時は最後の試行結果)
          "attempts": int,       # 試行回数
          "errors": [...],       # 最終試行のエラー
          "warnings": [...],     # 最終試行の警告
          "needs_approval": bool, # 3回NGで承認待ちが必要
        }
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY が未設定")

    last_thread: list[dict] = []
    last_result = ValidationResult(ok=False, errors=["未試行"])
    for attempt in range(1, max_regen + 1):
        try:
            last_thread = _call_claude(input_data, api_key=api_key, model=model)
        except Exception as e:
            return {
                "ok": False,
                "thread": [],
                "attempts": attempt,
                "errors": [f"Claude API エラー: {e}"],
                "warnings": [],
                "needs_approval": False,
            }
        last_result = validate_thread(last_thread)
        if last_result.ok:
            return {
                "ok": True,
                "thread": last_thread,
                "attempts": attempt,
                "errors": [],
                "warnings": last_result.warnings,
                "needs_approval": False,
            }

    # 全試行NG → 承認待ちへ
    return {
        "ok": False,
        "thread": last_thread,
        "attempts": max_regen,
        "errors": last_result.errors,
        "warnings": last_result.warnings,
        "needs_approval": True,
    }


def article_to_input(
    article: Article,
    *,
    note_url: str | None = None,
    axis: str = "A",
    thread_length: int = 5,
    cta_variant: str = "free_trial",
    hashtags: list[str] | None = None,
) -> dict:
    """Article → generate_thread() 入力スキーマに変換。"""
    body = article.body or ""
    snippets = [s.strip() for s in body.split("\n") if s.strip()][:6]
    return {
        "topic": article.title,
        "axis": axis,
        "thread_length": thread_length,
        "source_article_url": note_url,
        "source_snippets": snippets,
        "hashtags": hashtags or ["#買取業界", "#出張買取"],
        "cta_variant": cta_variant,
    }


# ─────────────────────────────────────────────────────
# X 投稿（tweepy）
# ─────────────────────────────────────────────────────
def _notify(text: str) -> None:
    try:
        if TELEGRAM_NOTIFY.exists() and os.access(TELEGRAM_NOTIFY, os.X_OK):
            subprocess.run(
                [str(TELEGRAM_NOTIFY), text],
                timeout=10,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


def _build_client(config: XPublisherConfig):
    import tweepy

    return tweepy.Client(
        consumer_key=config.api_key,
        consumer_secret=config.api_secret,
        access_token=config.access_token,
        access_token_secret=config.access_token_secret,
        wait_on_rate_limit=False,
    )


def _post_one_tweet(
    client,
    text: str,
    in_reply_to_tweet_id: str | None = None,
    *,
    max_retries: int = 3,
) -> str:
    import tweepy

    last_error = None
    for attempt in range(max_retries):
        try:
            kwargs = {"text": text}
            if in_reply_to_tweet_id:
                kwargs["in_reply_to_tweet_id"] = in_reply_to_tweet_id
            response = client.create_tweet(**kwargs)
            return str(response.data["id"])
        except tweepy.TooManyRequests as e:
            last_error = e
            wait = 15 * 60
            _notify(f"⚠️ X API レート制限 429: {wait//60}分後リトライ")
            time.sleep(wait)
        except (tweepy.Unauthorized, tweepy.Forbidden) as e:
            _notify(f"🔑 X API 認証エラー({type(e).__name__}): {e}\nキー再発行が必要")
            raise
        except tweepy.TweepyException as e:
            last_error = e
            backoff = 2 ** attempt + random.uniform(0, 1)
            if attempt < max_retries - 1:
                time.sleep(backoff)
    raise RuntimeError(f"X API 投稿失敗（{max_retries}回リトライ後）: {last_error}")


# ─────────────────────────────────────────────────────
# 承認キュー
# ─────────────────────────────────────────────────────
def _ensure_file(path: Path) -> list[dict]:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("[]", encoding="utf-8")
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(f".broken.{int(time.time())}.json")
        path.rename(backup)
        path.write_text("[]", encoding="utf-8")
        return []


def _save_file(path: Path, entries: list[dict]) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def push_to_approval_queue(article: Article, attempt_result: dict, note_url: str | None) -> None:
    entries = _ensure_file(APPROVAL_QUEUE_FILE)
    entries.append({
        "queued_at": datetime.now().isoformat(),
        "article_title": article.title,
        "article_keyword": article.keyword,
        "note_url": note_url,
        "attempts": attempt_result.get("attempts"),
        "errors": attempt_result.get("errors"),
        "thread": attempt_result.get("thread"),
    })
    _save_file(APPROVAL_QUEUE_FILE, entries)
    _notify(
        f"⚠️ Xスレッド3回再生成失敗 → 承認待ち\n"
        f"記事: {article.title}\n"
        f"件数: {len(entries)}件保留中"
    )


# ─────────────────────────────────────────────────────
# メインAPI: スレッド作成
# ─────────────────────────────────────────────────────
def create_thread(
    article: Article,
    *,
    note_url: str | None = None,
    axis: str = "A",
    thread_length: int = 5,
    cta_variant: str = "free_trial",
    hashtags: list[str] | None = None,
    dry_run: bool = False,
    config: XPublisherConfig | None = None,
) -> dict:
    """記事をスレッド化して投稿（または dry_run でプレビュー）。

    Returns:
        {
          "success": bool,
          "dry_run": bool,
          "thread": [...],          # 生成された tweet 配列
          "tweet_ids": [str, ...],  # 投稿成功時のみ
          "first_tweet_url": str | None,
          "needs_approval": bool,
          "attempts": int,
          "error": str | None,
          "errors": [str, ...],
          "warnings": [str, ...],
          "posted_at": str | None,
          "note_url": str | None,
        }
    """
    config = config or XPublisherConfig.from_env()

    input_data = article_to_input(
        article,
        note_url=note_url,
        axis=axis,
        thread_length=thread_length,
        cta_variant=cta_variant,
        hashtags=hashtags,
    )

    # 1) 生成 + 検証 + 再生成
    try:
        gen = generate_thread(input_data, api_key=config.anthropic_api_key or None)
    except Exception as e:
        return {
            "success": False,
            "dry_run": dry_run,
            "thread": [],
            "tweet_ids": [],
            "first_tweet_url": None,
            "needs_approval": False,
            "attempts": 0,
            "error": f"スレッド生成失敗: {e}",
            "errors": [str(e)],
            "warnings": [],
            "posted_at": None,
            "note_url": note_url,
        }

    base_response = {
        "thread": gen["thread"],
        "attempts": gen["attempts"],
        "errors": gen["errors"],
        "warnings": gen["warnings"],
        "needs_approval": gen["needs_approval"],
        "note_url": note_url,
        "tweet_ids": [],
        "first_tweet_url": None,
        "posted_at": None,
    }

    if not gen["ok"]:
        # 3回NG → 承認キュー
        if gen["needs_approval"]:
            push_to_approval_queue(article, gen, note_url)
        return {
            **base_response,
            "success": False,
            "dry_run": dry_run,
            "error": "; ".join(gen["errors"][:3]),
        }

    if dry_run:
        return {**base_response, "success": True, "dry_run": True, "error": None}

    # 2) 本番投稿
    if not config.has_x_credentials():
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": "X認証情報が未設定（X_API_KEY/SECRET/X_ACCESS_TOKEN/SECRET 不足）",
        }
    if not config.enabled:
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": "X_SHARE_ENABLED=false（投稿スキップ）",
        }

    client = _build_client(config)
    tweet_ids: list[str] = []
    parent_id: str | None = None
    try:
        for i, t in enumerate(gen["thread"]):
            tid = _post_one_tweet(client, t["text"], in_reply_to_tweet_id=parent_id)
            tweet_ids.append(tid)
            parent_id = tid
            if i < len(gen["thread"]) - 1:
                time.sleep(random.uniform(0.5, 1.0))
        first_id = tweet_ids[0]
        return {
            **base_response,
            "success": True,
            "dry_run": False,
            "tweet_ids": tweet_ids,
            "first_tweet_url": f"https://x.com/i/web/status/{first_id}",
            "posted_at": datetime.now().isoformat(),
            "error": None,
        }
    except Exception as e:
        _notify(
            f"❌ Xスレッド投稿失敗\n"
            f"記事: {article.title}\n"
            f"投稿済: {len(tweet_ids)}/{len(gen['thread'])}\n"
            f"エラー: {e}"
        )
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "tweet_ids": tweet_ids,
            "error": str(e),
        }


# ─────────────────────────────────────────────────────
# 投稿キュー（queue/x_posts.json）
# ─────────────────────────────────────────────────────
@dataclass
class XQueueEntry:
    scheduled_at: str
    article_id: str
    article_title: str
    note_url: str | None = None
    status: str = "pending"  # pending / posted / failed / skipped / needs_approval
    tweet_ids: list[str] = field(default_factory=list)
    error: str | None = None
    posted_at: str | None = None
    draft_path: str | None = None
    axis: str = "A"
    thread_length: int = 5
    cta_variant: str = "free_trial"

    def to_dict(self) -> dict:
        return asdict(self)


def enqueue(entry: XQueueEntry) -> None:
    entries = _ensure_file(QUEUE_FILE)
    entries.append(entry.to_dict())
    _save_file(QUEUE_FILE, entries)


def list_queue(status: str | None = None) -> list[dict]:
    entries = _ensure_file(QUEUE_FILE)
    if status is None:
        return entries
    return [e for e in entries if e.get("status") == status]


def update_entry(article_id: str, **fields) -> bool:
    entries = _ensure_file(QUEUE_FILE)
    updated = False
    for e in entries:
        if e.get("article_id") == article_id:
            e.update(fields)
            updated = True
    if updated:
        _save_file(QUEUE_FILE, entries)
    return updated


def pop_due_entries(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    entries = _ensure_file(QUEUE_FILE)
    due = []
    for e in entries:
        if e.get("status") != "pending":
            continue
        try:
            sched = datetime.fromisoformat(e["scheduled_at"])
        except (KeyError, ValueError):
            continue
        if sched <= now:
            due.append(e)
    return due


def list_approval_queue() -> list[dict]:
    return _ensure_file(APPROVAL_QUEUE_FILE)
