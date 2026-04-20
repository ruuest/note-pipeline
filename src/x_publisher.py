"""X(Twitter) スレッド自動投稿（Playwright スクレイピング方式）。

note記事を Claude Sonnet 4.6 で 3〜7 ツイートのスレッドに分割し、
Playwright で x.com にログインセッションを復元して順次投稿する。

【設計方針 v2.1 (2026-04-20) — ヒューマンライク強化】
- API 経路（tweepy）は廃止。.x-session.json を使った Playwright スクレイピング。
- **ボット検知回避のためのヒューマンライク動作**:
  - 1文字ずつタイピング（40〜180ms/char + 句読点後の考える間 + 迷いポーズ）
  - 固定 sleep 全廃、全てランダムジッター
  - マウスはベジェ風迂回 → ホバー → down/up 分解
  - プリ行動: home でスクロール滞在、TL クリック→戻る（30%確率）
  - ポスト行動: プロフィール滞在
  - フィンガープリント固定: macOS Chrome UA / 1440x900 / ja-JP / Asia/Tokyo
  - playwright-stealth で webdriver 等 JS 検知を封じる
- タイミングは logs/x_timing_*.log に記録（分布確認用）
- note publisher (src/publisher.py) と同じ session エラーハンドリング
- 失敗時 logs/screenshots/ にスクショ保存
- 未ログイン/期限切れは XSessionError で Telegram 通知
- .env の X_BEARER_TOKEN 等は optional（将来のフォールバック用に残置）。
- 3回連続失敗で 24 時間クールダウン（.x-cooldown.lock で管理）。

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

import asyncio
import json
import math
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path

from src.models import Article

BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "queue"
QUEUE_FILE = QUEUE_DIR / "x_posts.json"
APPROVAL_QUEUE_FILE = QUEUE_DIR / "x_approval_queue.json"
PROMPT_PATH = BASE_DIR / "docs" / "x_thread_prompt.md"
TONE_GUIDE_PATH = BASE_DIR / "docs" / "x_tone_guide.md"
COMPLIANCE_RULES_PATH = BASE_DIR / "config" / "compliance_rules.yaml"
SESSION_PATH = BASE_DIR / ".x-session.json"
COOLDOWN_FILE = BASE_DIR / ".x-cooldown.lock"
FAILURE_LOG = BASE_DIR / ".x-failures.log"
TIMING_LOG_DIR = BASE_DIR / "logs"
SCREENSHOTS_DIR = BASE_DIR / "logs" / "screenshots"
TELEGRAM_NOTIFY = Path("/Users/apple/NorthValueAsset/cabinet/scripts/telegram_notify.sh")

# ─── ヒューマンライク動作パラメータ ───
TYPE_CHAR_DELAY_MIN = 0.04  # 秒/文字（最小）
TYPE_CHAR_DELAY_MAX = 0.18  # 秒/文字（最大）
TYPE_PAUSE_AFTER_PUNCT_MIN = 0.2  # 句読点後の考える間
TYPE_PAUSE_AFTER_PUNCT_MAX = 0.6
TYPE_HESITATION_PROB = 0.05  # 5%確率で
TYPE_HESITATION_MIN = 0.5  # 迷いポーズ
TYPE_HESITATION_MAX = 1.5

JITTER_PAGE_TRANSITION_MIN = 2.0  # 画面遷移後
JITTER_PAGE_TRANSITION_MAX = 4.5
JITTER_ADD_SLOT_MIN = 1.5  # + 枠追加の間
JITTER_ADD_SLOT_MAX = 3.5
JITTER_PRE_SUBMIT_MIN = 3.0  # 投稿ボタン前
JITTER_PRE_SUBMIT_MAX = 6.0

PRE_ACTION_SCROLL_COUNT = (2, 4)  # ランダム回数
PRE_ACTION_DWELL_MIN = 5.0
PRE_ACTION_DWELL_MAX = 15.0
POST_ACTION_DWELL_MIN = 8.0
POST_ACTION_DWELL_MAX = 20.0
TL_CLICK_PROB = 0.30

HOVER_BEFORE_CLICK_MIN = 0.05  # 50ms
HOVER_BEFORE_CLICK_MAX = 0.20  # 200ms
MOUSE_PATH_STEPS = (3, 5)  # ベジェ風経路ステップ数

# 失敗/クールダウン
MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_DURATION_SEC = 24 * 3600
RATE_LIMIT_WAIT_MIN = 30 * 60  # 30分
RATE_LIMIT_WAIT_MAX = 60 * 60  # 60分

# 投稿時刻ジッター（scheduler 側で使用）
SCHEDULE_JITTER_SEC = 20 * 60  # ±20分
MIN_INTERVAL_BETWEEN_POSTS_SEC = 2 * 3600  # 同一日 最低2時間

# フィンガープリント（macOS Chrome 最新版 実在 UA、2026-04 時点）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_LOCALE = "ja-JP"
DEFAULT_TIMEZONE = "Asia/Tokyo"

# X UI 制約（投稿本体の制限値は従来通り）
TWEET_HARD_LIMIT = 280
TWEET_SAFE_LIMIT = 135
THREAD_MIN = 3
THREAD_MAX = 7
EMOJI_PER_TWEET_MAX = 2
DEFAULT_CTA_URL = "https://nvcloud-lp.pages.dev/"
ALLOWED_URL = "https://nvcloud-lp.pages.dev/"

X_PROFILE_URL = "https://x.com/home"
X_COMPOSE_URL = "https://x.com/compose/post"

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_REGEN = 3

# レート制限相当（連続投稿検知）— UI 側の "制限" ダイアログ検知で使用
RATE_LIMIT_WAIT_SEC = 30 * 60  # 30分 sleep でリトライ

# CTA バリエーション
CTA_VARIANTS = {
    "free_trial": f"機能詳細+無料体験はこちら → {ALLOWED_URL}",
    "doc_dl": f"資料ダウンロードはLPから → {ALLOWED_URL}",
    "inquiry": f"導入相談はLPのお問い合わせフォームから → {ALLOWED_URL}",
}


class XSessionError(RuntimeError):
    """X セッション関連の致命的エラー（再ログイン必須）。"""


class XCooldownError(RuntimeError):
    """連続失敗クールダウン中。投稿スキップ。"""


# ─────────────────────────────────────────────────────
# 失敗カウント / クールダウン
# ─────────────────────────────────────────────────────
def _read_failure_count() -> int:
    if not FAILURE_LOG.exists():
        return 0
    try:
        return int(FAILURE_LOG.read_text(encoding="utf-8").strip() or "0")
    except (ValueError, OSError):
        return 0


def _write_failure_count(n: int) -> None:
    try:
        FAILURE_LOG.write_text(str(n), encoding="utf-8")
    except OSError:
        pass


def _reset_failure_count() -> None:
    try:
        if FAILURE_LOG.exists():
            FAILURE_LOG.unlink()
    except OSError:
        pass


def _in_cooldown() -> bool:
    if not COOLDOWN_FILE.exists():
        return False
    try:
        until = datetime.fromisoformat(COOLDOWN_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    if datetime.now() < until:
        return True
    # クールダウン解除
    try:
        COOLDOWN_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    _reset_failure_count()
    return False


def _enter_cooldown(reason: str) -> None:
    until = datetime.now() + timedelta(seconds=COOLDOWN_DURATION_SEC)
    try:
        COOLDOWN_FILE.write_text(until.isoformat(), encoding="utf-8")
    except OSError:
        pass
    _notify(
        f"🛑 X投稿 24時間クールダウン開始\n"
        f"理由: {reason}\n"
        f"再開: {until.strftime('%Y-%m-%d %H:%M')}"
    )


def _record_failure(reason: str) -> None:
    cnt = _read_failure_count() + 1
    _write_failure_count(cnt)
    if cnt >= MAX_CONSECUTIVE_FAILURES:
        _enter_cooldown(f"連続{cnt}回失敗: {reason}")


def _record_success() -> None:
    _reset_failure_count()


# ─────────────────────────────────────────────────────
# 設定 / 認証
# ─────────────────────────────────────────────────────
@dataclass
class XPublisherConfig:
    """X 投稿設定。

    API認証情報（api_key等）は optional — Phase 1 はスクレイピング方式なので未使用。
    enabled=True かつ SESSION_PATH が存在すれば投稿可能。
    anthropic_api_key はスレッド生成に必須。
    """
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""
    enabled: bool = False
    anthropic_api_key: str = ""
    headless: bool = False  # cron 実行時は True を推奨（UI変動リスクあり、要検討）

    @classmethod
    def from_env(cls) -> "XPublisherConfig":
        return cls(
            api_key=os.environ.get("X_API_KEY", ""),
            api_secret=os.environ.get("X_API_SECRET", ""),
            access_token=os.environ.get("X_ACCESS_TOKEN", ""),
            access_token_secret=os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
            enabled=os.environ.get("X_SHARE_ENABLED", "false").lower() == "true",
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            headless=os.environ.get("X_HEADLESS", "false").lower() == "true",
        )

    def has_x_credentials(self) -> bool:
        """スクレイピング方式では session ファイルの有無を返す。"""
        return SESSION_PATH.exists()

    def is_ready(self) -> bool:
        return self.enabled and self.has_x_credentials()


# ─────────────────────────────────────────────────────
# システムプロンプト読み込み
# ─────────────────────────────────────────────────────
def load_system_prompt() -> str:
    """docs/x_thread_prompt.md から ```` で囲まれたシステムプロンプトを抽出。"""
    if not PROMPT_PATH.exists():
        return (
            "あなたはNV CLOUDの公式X投稿スレッドを生成するコピーライターです。"
            "JSON配列のみで返却してください。"
        )
    text = PROMPT_PATH.read_text(encoding="utf-8")
    match = re.search(r"````\s*\n(.*?)\n````", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


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

        if len(body_no_url) > TWEET_SAFE_LIMIT:
            errors.append(
                f"index={idx}: 本文 {len(body_no_url)}字、上限{TWEET_SAFE_LIMIT}字超過"
            )
        if len(text) > TWEET_HARD_LIMIT:
            errors.append(
                f"index={idx}: 全体 {len(text)}字、X上限{TWEET_HARD_LIMIT}字超過"
            )
        emj = count_emoji(text)
        if emj > EMOJI_PER_TWEET_MAX:
            errors.append(f"index={idx}: 絵文字 {emj}個、上限{EMOJI_PER_TWEET_MAX}個超過")
        e, w = _check_compliance(text, rules)
        for m in e:
            errors.append(f"index={idx}: {m}")
        for m in w:
            warnings.append(f"index={idx}: {m}")

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
# 通知 / ファイル操作
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
# Playwright XPublisher（スクレイピング方式）
# ─────────────────────────────────────────────────────
class XPublisher:
    """Playwright で x.com に投稿する（ヒューマンライク動作）。

    責務:
      - .x-session.json から BrowserContext を復元（UA/viewport/locale/tz 固定）
      - playwright-stealth で navigator.webdriver 等を偽装
      - ホームで事前スクロール → Post ダイアログ → 1文字ずつタイピング
      - マウス操作はベジェ風迂回 + ホバー + down/up 分解
      - スレッド: 本文入力 → "+" で次枠追加 → 全て入力後 "Post all"
      - 失敗時は logs/screenshots/ にスクショ保存 + Telegram 通知
      - レート制限ダイアログ検知で 30〜60 分 sleep リトライ
      - 3 回連続失敗で 24 時間クールダウン
      - タイミングは logs/x_timing_*.log に記録
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self._timing_log: list[tuple[str, float]] = []
        self._timing_session = datetime.now().strftime("%Y%m%d_%H%M%S")

    async def start(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        # Chrome っぽい起動引数（自動化検知を緩和）
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

    async def stop(self):
        self._flush_timing_log()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    # ─── Timing log ────────────────────────────────
    def _log_timing(self, label: str, seconds: float) -> None:
        self._timing_log.append((label, seconds))

    def _flush_timing_log(self) -> None:
        if not self._timing_log:
            return
        try:
            TIMING_LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = TIMING_LOG_DIR / f"x_timing_{self._timing_session}.log"
            with open(path, "a", encoding="utf-8") as f:
                for label, sec in self._timing_log:
                    f.write(f"{datetime.now().isoformat()}\t{label}\t{sec:.3f}\n")
            self._timing_log.clear()
        except OSError:
            pass

    # ─── Jitter / human-like helpers ───────────────
    async def _jitter_sleep(self, page, low: float, high: float, label: str = "jitter") -> None:
        wait = random.uniform(low, high)
        self._log_timing(label, wait)
        await page.wait_for_timeout(int(wait * 1000))

    async def _human_type(self, page, text: str) -> None:
        """1文字ずつ人間のようにタイピング。

        - 各文字 40〜180ms
        - 句読点/改行後は 200〜600ms の「考える間」
        - 5% 確率で 500〜1500ms の「迷いポーズ」
        """
        start = time.monotonic()
        for ch in text:
            # 迷いポーズ（発生確率低）
            if random.random() < TYPE_HESITATION_PROB:
                pause = random.uniform(TYPE_HESITATION_MIN, TYPE_HESITATION_MAX)
                self._log_timing("type_hesitation", pause)
                await page.wait_for_timeout(int(pause * 1000))

            # 1文字入力
            await page.keyboard.type(ch)

            # 文字間ディレイ
            delay = random.uniform(TYPE_CHAR_DELAY_MIN, TYPE_CHAR_DELAY_MAX)
            await page.wait_for_timeout(int(delay * 1000))

            # 句読点/改行後の考える間
            if ch in ("。", "、", "\n", "！", "？", "!", "?"):
                pause = random.uniform(
                    TYPE_PAUSE_AFTER_PUNCT_MIN, TYPE_PAUSE_AFTER_PUNCT_MAX
                )
                self._log_timing("type_punct_pause", pause)
                await page.wait_for_timeout(int(pause * 1000))
        elapsed = time.monotonic() - start
        self._log_timing(f"type_total_{len(text)}ch", elapsed)

    async def _human_mouse_move_to(self, page, target_x: float, target_y: float) -> None:
        """現在位置から target に向かってベジェ風に 3〜5 ステップで迂回。"""
        steps = random.randint(*MOUSE_PATH_STEPS)
        # 現在位置は知らないので左上からとみなす（Playwright の mouse は
        # 最後の move 位置を保持する）。中継点はランダム曲率で生成。
        start_x = random.uniform(100, 400)
        start_y = random.uniform(100, 400)
        # コントロール点（ベジェ曲線風）
        ctrl_x = (start_x + target_x) / 2 + random.uniform(-120, 120)
        ctrl_y = (start_y + target_y) / 2 + random.uniform(-120, 120)
        for i in range(1, steps + 1):
            t = i / steps
            # 2次ベジェ: B(t) = (1-t)^2 P0 + 2(1-t)t P1 + t^2 P2
            x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t**2 * target_x
            y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t**2 * target_y
            try:
                await page.mouse.move(x, y)
            except Exception:
                pass
            await page.wait_for_timeout(random.randint(20, 80))

    async def _human_click(self, page, selector: str, *, timeout: int = 5000) -> bool:
        """マウス迂回 → ホバー → down/up 分解クリック。"""
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                return False
            try:
                await loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            box = await loc.bounding_box(timeout=timeout)
            if not box:
                # bounding_box が取れない場合は通常クリック
                await loc.click(timeout=timeout)
                return True
            # クリック位置は要素内ランダム
            target_x = box["x"] + random.uniform(box["width"] * 0.3, box["width"] * 0.7)
            target_y = box["y"] + random.uniform(
                box["height"] * 0.3, box["height"] * 0.7
            )
            await self._human_mouse_move_to(page, target_x, target_y)
            # ホバー
            hover = random.uniform(HOVER_BEFORE_CLICK_MIN, HOVER_BEFORE_CLICK_MAX)
            await page.wait_for_timeout(int(hover * 1000))
            # down → up 分解
            await page.mouse.down()
            await page.wait_for_timeout(random.randint(30, 120))
            await page.mouse.up()
            return True
        except Exception:
            return False

    async def _click_first_available_human(
        self, page, selectors: list[str], *, timeout: int = 3000
    ) -> bool:
        """セレクタ候補を順に試し、最初にヒットしたものを人間風クリック。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    ok = await self._human_click(page, sel, timeout=timeout)
                    if ok:
                        return True
            except Exception:
                continue
        return False

    # ─── Session / Screenshots ─────────────────────
    def _notify_session_issue(self, detail: str) -> None:
        _notify(
            "🔑 X セッション切れ、再ログインが必要\n"
            f"原因: {detail}\n"
            "対処: /Users/apple/NorthValueAsset/note-pipeline/scripts/x_auth_init.sh を実行"
        )

    async def _save_screenshot(self, page, tag: str) -> Path | None:
        try:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = SCREENSHOTS_DIR / f"x_{tag}_{ts}.png"
            await page.screenshot(path=str(out))
            return out
        except Exception:
            return None

    async def _apply_stealth(self, context) -> None:
        """playwright-stealth で JS 側フィンガープリント偽装。失敗は握りつぶす。"""
        try:
            from playwright_stealth import Stealth

            stealth = Stealth()
            await stealth.apply_stealth_async(context)
        except Exception as e:
            print(f"  ⚠ stealth 適用失敗（投稿は続行）: {e}")

    async def _get_context(self):
        """保存済みセッションから BrowserContext を復元（フィンガープリント固定）。"""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        if not SESSION_PATH.exists():
            self._notify_session_issue(f"{SESSION_PATH.name} が存在しない（未ログイン）")
            raise XSessionError(
                f"{SESSION_PATH} が存在しません。scripts/x_auth_init.sh で再認証してください。"
            )

        context = await self.browser.new_context(
            storage_state=str(SESSION_PATH),
            user_agent=DEFAULT_USER_AGENT,
            viewport=DEFAULT_VIEWPORT,
            locale=DEFAULT_LOCALE,
            timezone_id=DEFAULT_TIMEZONE,
        )
        # navigator.webdriver 偽装 + 小道具
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});"
            "Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});"
        )
        await self._apply_stealth(context)

        page = await context.new_page()
        try:
            await page.goto(X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
            await self._jitter_sleep(
                page,
                JITTER_PAGE_TRANSITION_MIN,
                JITTER_PAGE_TRANSITION_MAX,
                "session_check_load",
            )
            url = page.url
            if "/login" in url or "/i/flow/login" in url:
                await self._save_screenshot(page, "session_expired")
                await page.close()
                await context.close()
                self._notify_session_issue("home 遷移で /login にリダイレクトされた")
                raise XSessionError(
                    "X セッションが期限切れです。scripts/x_auth_init.sh で再認証してください。"
                )
            await page.close()
            return context
        except PlaywrightTimeoutError as e:
            await self._save_screenshot(page, "session_timeout")
            try:
                await page.close()
            except Exception:
                pass
            await context.close()
            print(f"  ⚠ X セッション検証が Timeout: {e}")
            raise
        except XSessionError:
            raise
        except Exception as e:
            await self._save_screenshot(page, "session_unknown")
            try:
                await page.close()
            except Exception:
                pass
            await context.close()
            print(f"  ⚠ X セッション検証中の不明エラー: {e}")
            raise

    # ─── Pre / Post 行動 ────────────────────────────
    async def _warmup_home(self, page) -> None:
        """投稿前に home でスクロール滞在。"""
        scroll_count = random.randint(*PRE_ACTION_SCROLL_COUNT)
        for _ in range(scroll_count):
            try:
                delta = random.randint(400, 900)
                await page.mouse.wheel(0, delta)
            except Exception:
                pass
            await self._jitter_sleep(page, 0.8, 2.2, "pre_scroll")
        await self._jitter_sleep(
            page, PRE_ACTION_DWELL_MIN, PRE_ACTION_DWELL_MAX, "pre_dwell"
        )

    async def _browse_timeline(self, page) -> None:
        """30%確率で TL のツイートを1つ開いて戻る。"""
        if random.random() > TL_CLICK_PROB:
            return
        try:
            article = page.locator('article[data-testid="tweet"]').first
            if await article.count() == 0:
                return
            box = await article.bounding_box(timeout=2000)
            if not box:
                return
            tx = box["x"] + box["width"] * 0.5
            ty = box["y"] + box["height"] * 0.3
            await self._human_mouse_move_to(page, tx, ty)
            await page.wait_for_timeout(random.randint(100, 300))
            try:
                await article.click(timeout=3000)
            except Exception:
                return
            await self._jitter_sleep(page, 2.5, 5.0, "tl_dwell")
            await page.go_back(wait_until="domcontentloaded", timeout=30000)
            await self._jitter_sleep(page, 1.0, 2.5, "tl_back")
        except Exception:
            pass

    async def _post_action_dwell(self, context) -> None:
        """投稿後、プロフィール画面に滞在。"""
        try:
            page = await context.new_page()
            await page.goto(X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
            await self._jitter_sleep(page, 1.5, 3.0, "post_action_load")
            for _ in range(random.randint(1, 3)):
                try:
                    await page.mouse.wheel(0, random.randint(300, 700))
                except Exception:
                    pass
                await self._jitter_sleep(page, 0.8, 2.0, "post_scroll")
            await self._jitter_sleep(
                page, POST_ACTION_DWELL_MIN, POST_ACTION_DWELL_MAX, "post_dwell"
            )
            try:
                await page.close()
            except Exception:
                pass
        except Exception:
            pass

    # ─── UI 操作 ────────────────────────────────
    # セレクタは多段 fallback:
    #   1. data-testid（最安定） → 2. aria-label（日英） → 3. role+属性 → 4. text/placeholder
    # X は UI を頻繁に変更するため、1つがダメでも次で拾える構造を維持する。

    async def _fill_textbox_human(self, page, tweet_text: str, index: int) -> bool:
        """index 番目（0-origin）のツイート本文入力欄にテキストを人間風に入力。"""
        selectors = [
            # data-testid（最安定）
            'div[role="textbox"][data-testid^="tweetTextarea_"]',
            f'div[data-testid="tweetTextarea_{index}"]',
            'div[data-testid^="tweetTextarea_"]',
            # aria-label（日英両対応、2026-04 X UI）
            'div[role="textbox"][aria-label*="Post text"]',
            'div[role="textbox"][aria-label*="Post your reply"]',
            'div[role="textbox"][aria-label*="Post"]',
            'div[role="textbox"][aria-label*="ポスト本文"]',
            'div[role="textbox"][aria-label*="ポストする内容"]',
            'div[role="textbox"][aria-label*="ポスト"]',
            'div[role="textbox"][aria-label*="何か"]',
            # 汎用 contenteditable
            'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"][data-text="true"]',
        ]
        textbox = None
        hit_selector = None
        for sel in selectors:
            try:
                loc = page.locator(sel)
                cnt = await loc.count()
                if cnt > index:
                    textbox = loc.nth(index)
                    hit_selector = sel
                    break
            except Exception:
                continue
        if textbox is None or hit_selector is None:
            return False
        try:
            # クリック領域にマウス移動
            box = await textbox.bounding_box(timeout=3000)
            if box:
                tx = box["x"] + random.uniform(box["width"] * 0.2, box["width"] * 0.8)
                ty = box["y"] + random.uniform(box["height"] * 0.3, box["height"] * 0.7)
                await self._human_mouse_move_to(page, tx, ty)
                await page.wait_for_timeout(random.randint(50, 150))
                try:
                    await textbox.click(timeout=3000)
                except Exception:
                    await page.mouse.click(tx, ty)
            else:
                await textbox.click(timeout=3000)
            await page.wait_for_timeout(random.randint(150, 400))
            await self._human_type(page, tweet_text)
            await self._jitter_sleep(page, 0.3, 0.8, "after_type")
            return True
        except Exception:
            return False

    async def _add_thread_slot_human(self, page) -> bool:
        selectors = [
            # data-testid（最安定）
            'button[data-testid="addButton"]',
            'div[data-testid="addButton"]',
            '[data-testid="addButton"]',
            # aria-label（日英）
            'button[aria-label*="Add post"]',
            'button[aria-label*="Add another post"]',
            'button[aria-label*="ポストを追加"]',
            'button[aria-label*="投稿を追加"]',
            'button[aria-label*="Add"]',
            'div[role="button"][aria-label*="Add post"]',
            'div[role="button"][aria-label*="Add"]',
            'div[role="button"][aria-label*="追加"]',
        ]
        return await self._click_first_available_human(page, selectors, timeout=3000)

    async def _click_post_all_human(self, page) -> bool:
        selectors = [
            # data-testid（最安定、送信本体）
            'button[data-testid="tweetButtonInline"]',
            'button[data-testid="tweetButton"]',
            'div[data-testid="tweetButtonInline"]',
            'div[data-testid="tweetButton"]',
            # aria-label（2026-04 X UI は aria-label に「すべてポスト」「ポスト」を使う）
            'button[aria-label*="Post all"]',
            'button[aria-label*="すべてポスト"]',
            'button[aria-label*="すべて投稿"]',
            'button[aria-label="Post"]',
            'button[aria-label="ポスト"]',
            'button[aria-label="ポストする"]',
            # text（最後の砦）
            'button:has-text("Post all")',
            'button:has-text("すべてポスト")',
            'button:has-text("すべて投稿")',
            'button:has-text("ポストする")',
            'button:has-text("投稿する")',
            'button:has-text("Post")',
            'button:has-text("ポスト")',
        ]
        return await self._click_first_available_human(page, selectors, timeout=5000)

    # Post ボタンを「クリックせず存在確認のみ」するためのヘルパ（test_x_selectors.py 用）
    POST_ALL_SELECTORS: list[str] = [
        'button[data-testid="tweetButtonInline"]',
        'button[data-testid="tweetButton"]',
        'div[data-testid="tweetButtonInline"]',
        'div[data-testid="tweetButton"]',
        'button[aria-label*="Post all"]',
        'button[aria-label*="すべてポスト"]',
        'button[aria-label*="すべて投稿"]',
        'button[aria-label="Post"]',
        'button[aria-label="ポスト"]',
        'button[aria-label="ポストする"]',
        'button:has-text("Post all")',
        'button:has-text("すべてポスト")',
        'button:has-text("すべて投稿")',
        'button:has-text("ポストする")',
        'button:has-text("投稿する")',
        'button:has-text("Post")',
        'button:has-text("ポスト")',
    ]

    COMPOSE_OPEN_SELECTORS: list[str] = [
        # data-testid（最安定、X 伝統的に SideNav_NewTweet_Button）
        'a[data-testid="SideNav_NewTweet_Button"]',
        'button[data-testid="SideNav_NewTweet_Button"]',
        '[data-testid="SideNav_NewTweet_Button"]',
        'a[data-testid="FloatingActionButtons_Tweet_Button"]',
        '[data-testid="FloatingActionButtons_Tweet_Button"]',
        # href（レガシー・新UIでも一部残存）
        'a[href="/compose/post"]',
        'a[href="/compose/tweet"]',
        'a[href*="/compose/"]',
        # aria-label（日英）
        'a[aria-label="Post"]',
        'a[aria-label="ポストする"]',
        'a[aria-label="投稿する"]',
        'a[aria-label*="Post"]',
        'a[aria-label*="ポスト"]',
        'button[aria-label="Post"]',
        'button[aria-label*="Post"]',
        'button[aria-label*="ポスト"]',
        # navigation 配下のリンク
        'nav[role="navigation"] a[href*="/compose"]',
        'div[role="navigation"] a[href*="/compose"]',
    ]

    TWEET_TEXTAREA_SELECTORS: list[str] = [
        'div[role="textbox"][data-testid^="tweetTextarea_"]',
        'div[data-testid="tweetTextarea_0"]',
        'div[data-testid^="tweetTextarea_"]',
        'div[role="textbox"][aria-label*="Post text"]',
        'div[role="textbox"][aria-label*="Post"]',
        'div[role="textbox"][aria-label*="ポスト本文"]',
        'div[role="textbox"][aria-label*="ポスト"]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"][data-text="true"]',
    ]

    ADD_SLOT_SELECTORS: list[str] = [
        'button[data-testid="addButton"]',
        'div[data-testid="addButton"]',
        '[data-testid="addButton"]',
        'button[aria-label*="Add post"]',
        'button[aria-label*="ポストを追加"]',
        'button[aria-label*="投稿を追加"]',
        'button[aria-label*="Add"]',
        'div[role="button"][aria-label*="Add"]',
        'div[role="button"][aria-label*="追加"]',
    ]

    TWEET_ARTICLE_SELECTORS: list[str] = [
        'article[data-testid="tweet"]',
        'article[role="article"][data-testid*="tweet"]',
        'article[role="article"]',
    ]

    IMAGE_UPLOAD_SELECTORS: list[str] = [
        'input[data-testid="fileInput"]',
        'input[type="file"][accept*="image"]',
        'button[data-testid="attachments"]',
        'button[aria-label*="Media"]',
        'button[aria-label*="メディア"]',
    ]

    MODAL_CLOSE_SELECTORS: list[str] = [
        'button[data-testid="app-bar-close"]',
        'button[aria-label="Close"]',
        'button[aria-label="閉じる"]',
        'div[role="button"][aria-label="Close"]',
        'div[role="button"][aria-label="閉じる"]',
    ]

    async def _detect_rate_limit(self, page) -> bool:
        try:
            patterns = [
                "rate limit",
                "too many",
                "制限",
                "しばらくしてから",
                "上限に達しました",
            ]
            html = (await page.content()).lower()
            return any(p.lower() in html for p in patterns)
        except Exception:
            return False

    async def _open_compose(self, context):
        """home で warmup → TL 閲覧（確率）→ 新規ポストダイアログを開く。"""
        page = await context.new_page()
        await page.goto(X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
        await self._jitter_sleep(
            page,
            JITTER_PAGE_TRANSITION_MIN,
            JITTER_PAGE_TRANSITION_MAX,
            "home_load",
        )
        # プリ行動
        await self._warmup_home(page)
        await self._browse_timeline(page)

        opened = await self._click_first_available_human(
            page,
            self.COMPOSE_OPEN_SELECTORS,
            timeout=3000,
        )
        if not opened:
            # サイドバーのボタンが見つからない時は URL 直打ちで compose ダイアログを開く
            # （2026-04 X UI 変更への保険。テキストエリアは /compose/post でも出る）
            await page.goto(X_COMPOSE_URL, wait_until="domcontentloaded", timeout=60000)
            await self._jitter_sleep(
                page,
                JITTER_PAGE_TRANSITION_MIN,
                JITTER_PAGE_TRANSITION_MAX,
                "compose_fallback_load",
            )
        else:
            await self._jitter_sleep(page, 1.2, 2.5, "compose_open")
        return page

    async def _fetch_latest_tweet_id(self, context) -> str | None:
        page = await context.new_page()
        try:
            await page.goto(X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
            await self._jitter_sleep(page, 2.5, 4.5, "fetch_latest_load")
            links = page.locator('a[href*="/status/"]')
            count = await links.count()
            for i in range(min(count, 10)):
                href = await links.nth(i).get_attribute("href")
                if not href:
                    continue
                m = re.search(r"/status/(\d+)", href)
                if m:
                    return m.group(1)
            return None
        except Exception:
            return None
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def post_thread(self, thread: list[dict], *, dry_run: bool = False) -> dict:
        """スレッドを投稿（ヒューマンライク動作）。

        Args:
            thread: ツイート配列
            dry_run: True の場合、最終の送信クリック直前で return（誤投稿防止）。
                     テキスト入力・Add 枠追加までは全て実行し、UI フローを検証できる。

        Returns:
            {"success": bool, "tweet_ids": [...], "error": str|None, "rate_limited": bool, "dry_run": bool}
        """
        if not thread:
            return {"success": False, "tweet_ids": [], "error": "thread is empty", "dry_run": dry_run}

        overall_start = time.monotonic()
        context = await self._get_context()
        page = await self._open_compose(context)
        try:
            # 1本目
            if not await self._fill_textbox_human(page, thread[0]["text"], 0):
                await self._save_screenshot(page, "fill_failed_0")
                raise RuntimeError("1本目の入力欄が見つかりません（UI変化の可能性）")

            # 2本目以降は + で枠追加 → 入力
            for i in range(1, len(thread)):
                await self._jitter_sleep(
                    page, JITTER_ADD_SLOT_MIN, JITTER_ADD_SLOT_MAX, "add_slot_before"
                )
                if not await self._add_thread_slot_human(page):
                    await self._save_screenshot(page, f"add_slot_failed_{i}")
                    raise RuntimeError(f"スレッド追加ボタン（+）が見つかりません index={i}")
                await self._jitter_sleep(page, 0.6, 1.4, "add_slot_after")
                if not await self._fill_textbox_human(page, thread[i]["text"], i):
                    await self._save_screenshot(page, f"fill_failed_{i}")
                    raise RuntimeError(f"index={i} の入力欄が見つかりません")

            await self._save_screenshot(page, "pre_post")

            # 投稿前の間
            await self._jitter_sleep(
                page, JITTER_PRE_SUBMIT_MIN, JITTER_PRE_SUBMIT_MAX, "pre_submit"
            )

            # ── dry_run: 送信ボタンの存在だけ検証して return（誤投稿防止）──
            if dry_run:
                # Post ボタンが DOM にあるかだけ確認し、絶対にクリックしない
                post_btn_found = False
                for sel in self.POST_ALL_SELECTORS:
                    try:
                        if await page.locator(sel).first.count() > 0:
                            post_btn_found = True
                            break
                    except Exception:
                        continue
                await self._save_screenshot(page, "dry_run_before_submit")
                return {
                    "success": True,
                    "tweet_ids": [],
                    "error": None,
                    "dry_run": True,
                    "post_button_found": post_btn_found,
                }

            if not await self._click_post_all_human(page):
                await self._save_screenshot(page, "post_button_not_found")
                raise RuntimeError("Post all ボタンが見つかりません")

            await self._jitter_sleep(page, 3.5, 6.5, "after_submit")

            if await self._detect_rate_limit(page):
                await self._save_screenshot(page, "rate_limit")
                wait = random.uniform(RATE_LIMIT_WAIT_MIN, RATE_LIMIT_WAIT_MAX)
                _notify(
                    f"⚠️ X レート制限検知、{int(wait//60)}分後リトライ予定"
                )
                return {
                    "success": False,
                    "tweet_ids": [],
                    "error": f"rate_limited (wait {int(wait)}s)",
                    "rate_limited": True,
                }

            await self._save_screenshot(page, "post_success")

            tweet_id = await self._fetch_latest_tweet_id(context)

            # ポスト行動
            await self._post_action_dwell(context)

            total = time.monotonic() - overall_start
            self._log_timing("post_thread_total", total)

            if tweet_id:
                return {"success": True, "tweet_ids": [tweet_id], "error": None, "elapsed_sec": total}
            return {"success": True, "tweet_ids": [], "error": None, "elapsed_sec": total}

        except XSessionError:
            raise
        except Exception as e:
            await self._save_screenshot(page, "post_exception")
            _notify(f"❌ Xスレッド投稿失敗\nエラー: {e}")
            return {"success": False, "tweet_ids": [], "error": str(e)}
        finally:
            try:
                await page.close()
            except Exception:
                pass
            await context.close()


async def _post_thread_async(thread: list[dict], headless: bool, dry_run: bool = False) -> dict:
    publisher = XPublisher(headless=headless)
    await publisher.start()
    try:
        return await publisher.post_thread(thread, dry_run=dry_run)
    finally:
        await publisher.stop()


def post_thread_sync(thread: list[dict], *, headless: bool = False, dry_run: bool = False) -> dict:
    """同期ラッパー。scheduler / publisher から呼ぶ。

    dry_run=True の場合、最終の送信クリック直前で return（誤投稿防止）。
    """
    return asyncio.run(_post_thread_async(thread, headless, dry_run))


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
    """記事をスレッド化して Playwright 経由で投稿（または dry_run でプレビュー）。

    Returns:
        {
          "success": bool,
          "dry_run": bool,
          "thread": [...],
          "tweet_ids": [str, ...],      # 先頭 tweet の id（後続は返信扱い）
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

    # 2) 本番投稿（Playwright）
    if not config.enabled:
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": "X_SHARE_ENABLED=false（投稿スキップ）",
        }
    if not SESSION_PATH.exists():
        _notify(
            "🔑 X セッション未設定、投稿スキップ\n"
            "対処: scripts/x_auth_init.sh を実行"
        )
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": f"{SESSION_PATH.name} 未作成（scripts/x_auth_init.sh を実行してください）",
        }

    # クールダウン中は投稿スキップ
    if _in_cooldown():
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": "X投稿クールダウン中（連続失敗により 24 時間停止中）",
        }

    try:
        result = post_thread_sync(gen["thread"], headless=config.headless)
    except XSessionError as e:
        _record_failure(f"session: {e}")
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": f"X session error: {e}",
        }
    except Exception as e:
        _record_failure(f"exception: {e}")
        _notify(
            f"❌ Xスレッド投稿失敗\n"
            f"記事: {article.title}\n"
            f"エラー: {e}"
        )
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": str(e),
        }

    if not result.get("success"):
        # レート制限は失敗カウントに含めない（一時的な制限）
        if not result.get("rate_limited"):
            _record_failure(result.get("error") or "投稿失敗")
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "tweet_ids": result.get("tweet_ids", []),
            "error": result.get("error") or "投稿失敗",
        }

    _record_success()
    tweet_ids = result.get("tweet_ids", [])
    first_url = (
        f"https://x.com/i/web/status/{tweet_ids[0]}" if tweet_ids else None
    )
    return {
        **base_response,
        "success": True,
        "dry_run": False,
        "tweet_ids": tweet_ids,
        "first_tweet_url": first_url,
        "posted_at": datetime.now().isoformat(),
        "error": None,
        "elapsed_sec": result.get("elapsed_sec"),
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


def apply_schedule_jitter(base: datetime, *, spread_sec: int = SCHEDULE_JITTER_SEC) -> datetime:
    """投稿予定時刻に ±spread_sec のランダムジッターを乗せる。"""
    offset = random.randint(-spread_sec, spread_sec)
    return base + timedelta(seconds=offset)


def enforce_min_interval(
    candidate: datetime,
    *,
    min_sec: int = MIN_INTERVAL_BETWEEN_POSTS_SEC,
) -> datetime:
    """同一日の既存投稿予定と最低 min_sec 離れるように候補時刻を調整。"""
    entries = _ensure_file(QUEUE_FILE)
    same_day = []
    for e in entries:
        if e.get("status") not in ("pending", "posted"):
            continue
        try:
            sched = datetime.fromisoformat(e["scheduled_at"])
        except (KeyError, ValueError):
            continue
        if sched.date() == candidate.date():
            same_day.append(sched)
    if not same_day:
        return candidate
    same_day.sort()
    result = candidate
    # 既存のどれかに近すぎれば min_sec 後ろにずらすを繰り返す
    changed = True
    while changed:
        changed = False
        for sched in same_day:
            if abs((result - sched).total_seconds()) < min_sec:
                result = sched + timedelta(seconds=min_sec + random.randint(0, 600))
                changed = True
                break
    return result


def list_approval_queue() -> list[dict]:
    return _ensure_file(APPROVAL_QUEUE_FILE)
