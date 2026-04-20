"""X(Twitter) スレッド自動投稿（Playwright スクレイピング方式）。

note記事を Claude Sonnet 4.6 で 3〜7 ツイートのスレッドに分割し、
Playwright で x.com にログインセッションを復元して順次投稿する。

【設計方針 v2.0 (2026-04-20)】
- API 経路（tweepy）は廃止。.x-session.json を使った Playwright スクレイピング。
- note publisher (src/publisher.py) と同じパターン:
  - storage_state で session 復元 → x.com/home
  - 失敗時 logs/screenshots/ にスクショ保存
  - 未ログイン/期限切れは XSessionError で Telegram 通知
- スレッドは UI の "+" / "追加" ボタンで入力欄を増やして一気に "Post all"。
- tweet_id は自分のプロフィール最新ツイートの URL 末尾から取得。
- セレクタは aria-label / data-testid / text の多段 fallback。
- .env の X_BEARER_TOKEN 等は optional（将来のフォールバック用に残置）。

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
import os
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
APPROVAL_QUEUE_FILE = QUEUE_DIR / "x_approval_queue.json"
PROMPT_PATH = BASE_DIR / "docs" / "x_thread_prompt.md"
TONE_GUIDE_PATH = BASE_DIR / "docs" / "x_tone_guide.md"
COMPLIANCE_RULES_PATH = BASE_DIR / "config" / "compliance_rules.yaml"
SESSION_PATH = BASE_DIR / ".x-session.json"
SCREENSHOTS_DIR = BASE_DIR / "logs" / "screenshots"
TELEGRAM_NOTIFY = Path("/Users/apple/NorthValueAsset/cabinet/scripts/telegram_notify.sh")

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
    """Playwright で x.com に投稿する。

    責務:
      - .x-session.json から BrowserContext を復元
      - ホームのポスト作成ダイアログ or /compose/post 経由で投稿
      - スレッド: 本文入力 → "+" で次枠追加 → 全て入力後 "Post all"
      - 失敗時は logs/screenshots/ にスクショ保存 + Telegram 通知
      - レート制限ダイアログ検知で 30 分 sleep リトライ
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.browser = None

    async def start(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=self.headless)

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

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

    async def _get_context(self):
        """保存済みセッションから BrowserContext を復元。"""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        if not SESSION_PATH.exists():
            self._notify_session_issue(f"{SESSION_PATH.name} が存在しない（未ログイン）")
            raise XSessionError(
                f"{SESSION_PATH} が存在しません。scripts/x_auth_init.sh で再認証してください。"
            )

        context = await self.browser.new_context(storage_state=str(SESSION_PATH))
        page = await context.new_page()
        try:
            await page.goto(X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            url = page.url
            # /login, /i/flow/login などにリダイレクトされたらセッション切れ
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
            print(f"  ⚠ X セッション検証が Timeout（セッション保全して中断）: {e}")
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

    async def _click_first_available(self, page, selectors: list[str], *, timeout: int = 3000) -> bool:
        """セレクタ候補を順に試して最初にヒットしたものをクリック。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
        return False

    async def _fill_textbox(self, page, tweet_text: str, index: int) -> bool:
        """index 番目（0-origin）のツイート本文入力欄にテキストを入れる。

        X の compose エディタは contenteditable な div[role="textbox"] で、
        スレッド追加時は複数表示される。
        """
        selectors = [
            'div[role="textbox"][data-testid^="tweetTextarea_"]',
            'div[role="textbox"][aria-label*="Post"]',
            'div[role="textbox"][aria-label*="ポスト"]',
            'div[role="textbox"][contenteditable="true"]',
        ]
        textbox = None
        for sel in selectors:
            try:
                loc = page.locator(sel)
                cnt = await loc.count()
                if cnt > index:
                    textbox = loc.nth(index)
                    break
            except Exception:
                continue
        if textbox is None:
            return False
        try:
            await textbox.click(timeout=3000)
            await page.wait_for_timeout(200)
            # type() は IME 等の影響を受けにくく、文字化けもしにくい
            await textbox.type(tweet_text, delay=5)
            await page.wait_for_timeout(300)
            return True
        except Exception:
            return False

    async def _add_thread_slot(self, page) -> bool:
        """スレッド追加ボタン（+）を押して次の入力枠を出す。"""
        selectors = [
            'button[data-testid="addButton"]',
            'button[aria-label*="Add post"]',
            'button[aria-label*="ポストを追加"]',
            'div[role="button"][aria-label*="Add"]',
        ]
        return await self._click_first_available(page, selectors, timeout=3000)

    async def _click_post_all(self, page) -> bool:
        """Post all ボタンを押して全投稿送信。"""
        selectors = [
            'button[data-testid="tweetButton"]',
            'button[data-testid="tweetButtonInline"]',
            'button:has-text("Post all")',
            'button:has-text("すべてポスト")',
            'button:has-text("すべて投稿")',
            'button:has-text("Post")',
            'button:has-text("ポスト")',
        ]
        return await self._click_first_available(page, selectors, timeout=5000)

    async def _detect_rate_limit(self, page) -> bool:
        """レート制限の警告が出ていないか。"""
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
        """ホームのポスト作成ダイアログを開く。

        /compose/post は仕様変更が起きやすいのでまず home を開いて
        "ポスト"/"Post" ボタンを押す。だめなら直接 /compose/post にフォールバック。
        """
        page = await context.new_page()
        await page.goto(X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        opened = await self._click_first_available(
            page,
            [
                'a[href="/compose/post"]',
                'a[data-testid="SideNav_NewTweet_Button"]',
                'button[data-testid="SideNav_NewTweet_Button"]',
                'a[aria-label*="Post"]',
                'a[aria-label*="ポスト"]',
            ],
            timeout=3000,
        )
        if not opened:
            await page.goto(X_COMPOSE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
        else:
            await page.wait_for_timeout(1500)
        return page

    async def _fetch_latest_tweet_id(self, context) -> str | None:
        """自分のプロフィール最新ツイートの URL 末尾 tweet_id を取得。"""
        page = await context.new_page()
        try:
            # home 直近の自分のポスト欄から status リンクを拾う
            await page.goto(X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3500)
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

    async def post_thread(self, thread: list[dict]) -> dict:
        """スレッドを投稿。

        Returns:
            {"success": bool, "tweet_ids": [...], "error": str|None}
            tweet_ids はスレッド先頭 tweet の id のみ（後続は返信扱いで個別取得不可）。
        """
        if not thread:
            return {"success": False, "tweet_ids": [], "error": "thread is empty"}

        context = await self._get_context()
        page = await self._open_compose(context)
        try:
            # 1本目
            if not await self._fill_textbox(page, thread[0]["text"], 0):
                await self._save_screenshot(page, "fill_failed_0")
                raise RuntimeError("1本目の入力欄が見つかりません（UI変化の可能性）")

            # 2本目以降は + で枠追加 → 入力
            for i in range(1, len(thread)):
                if not await self._add_thread_slot(page):
                    await self._save_screenshot(page, f"add_slot_failed_{i}")
                    raise RuntimeError(f"スレッド追加ボタン（+）が見つかりません index={i}")
                await page.wait_for_timeout(400)
                if not await self._fill_textbox(page, thread[i]["text"], i):
                    await self._save_screenshot(page, f"fill_failed_{i}")
                    raise RuntimeError(f"index={i} の入力欄が見つかりません")

            await self._save_screenshot(page, "pre_post")

            # Post all 押下
            if not await self._click_post_all(page):
                await self._save_screenshot(page, "post_button_not_found")
                raise RuntimeError("Post all ボタンが見つかりません")

            await page.wait_for_timeout(5000)

            # レート制限チェック
            if await self._detect_rate_limit(page):
                await self._save_screenshot(page, "rate_limit")
                _notify(f"⚠️ X レート制限検知、{RATE_LIMIT_WAIT_SEC//60}分後リトライ予定")
                return {
                    "success": False,
                    "tweet_ids": [],
                    "error": f"rate_limited (wait {RATE_LIMIT_WAIT_SEC}s)",
                    "rate_limited": True,
                }

            await self._save_screenshot(page, "post_success")

            tweet_id = await self._fetch_latest_tweet_id(context)
            if tweet_id:
                return {"success": True, "tweet_ids": [tweet_id], "error": None}
            # 投稿は通ったが tweet_id が拾えなかった
            return {"success": True, "tweet_ids": [], "error": None}

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


async def _post_thread_async(thread: list[dict], headless: bool) -> dict:
    publisher = XPublisher(headless=headless)
    await publisher.start()
    try:
        return await publisher.post_thread(thread)
    finally:
        await publisher.stop()


def post_thread_sync(thread: list[dict], *, headless: bool = False) -> dict:
    """同期ラッパー。scheduler / publisher から呼ぶ。"""
    return asyncio.run(_post_thread_async(thread, headless))


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

    try:
        result = post_thread_sync(gen["thread"], headless=config.headless)
    except XSessionError as e:
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "error": f"X session error: {e}",
        }
    except Exception as e:
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
        # レート制限の場合はキューに戻す判定は scheduler 側で実施
        return {
            **base_response,
            "success": False,
            "dry_run": False,
            "tweet_ids": result.get("tweet_ids", []),
            "error": result.get("error") or "投稿失敗",
        }

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
