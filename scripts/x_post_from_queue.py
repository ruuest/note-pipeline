"""X 単発投稿キューから 1 件投稿（自己修復型）。

失敗パターンを分類 → 自動修正 → 再試行 → 最終的に投稿完了 or 人間介入通知。

対応失敗パターン:
  1. TextareaNotFound   → 待機時間延長 + フォールバックセレクタ
  2. PostButtonNotFound → フォールバックセレクタ
  3. PostButtonDisabled → 本文の自動短縮（twitter_weight 基準で再試行）
  4. SessionExpired     → Telegram 通知 + status=needs_reauth（人間介入）
  5. Timeout/Network    → 指数バックオフで再試行
  6. その他例外         → スクショ + 再試行

最大 MAX_ATTEMPTS 回まで試行、それでも駄目なら Telegram 通知 + status=failed。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION = BASE_DIR / ".x-session.json"
QUEUE = BASE_DIR / "queue" / "x_individual_posts.json"
LOG_FILE = BASE_DIR / "logs" / "x_cron.log"
SCREENSHOT_DIR = BASE_DIR / "logs" / "screenshots"
TELEGRAM_NOTIFY = Path("/Users/apple/NorthValueAsset/cabinet/scripts/telegram_notify.sh")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

MAX_ATTEMPTS = 3
TWEET_LIMIT = 280
URL_WEIGHT = 23
URL_PATTERN = re.compile(
    r'https?://\S+|\b[a-zA-Z0-9][a-zA-Z0-9\-]*(?:\.[a-zA-Z0-9\-]+)+(?:/\S*)?'
)

# --- ログ / 通知 ---


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    line = f"[{stamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(msg: str) -> None:
    if TELEGRAM_NOTIFY.exists():
        try:
            subprocess.run([str(TELEGRAM_NOTIFY), msg], check=False, timeout=10)
        except Exception:
            pass


# --- 文字数カウント（twitter-text 準拠の簡易版）---


def twitter_weight(text: str) -> int:
    """X の投稿文字数カウントを近似。URL は 23 固定、非ASCIIは 2 倍。"""
    modified = text
    urls = URL_PATTERN.findall(text)
    for url in urls:
        modified = modified.replace(url, "", 1)
    weight = 0
    for c in modified:
        weight += 1 if ord(c) <= 0x007F else 2
    weight += URL_WEIGHT * len(urls)
    return weight


def shrink_text(text: str, target_weight: int = TWEET_LIMIT - 5) -> str:
    """末尾の段落から削って target_weight 以内に収める。URL 行は最後まで保持。"""
    paragraphs = text.split("\n\n")
    url_idx_set = {i for i, p in enumerate(paragraphs) if URL_PATTERN.search(p)}

    def compose(keep_set: set[int]) -> str:
        return "\n\n".join(p for i, p in enumerate(paragraphs) if i in keep_set)

    keep = set(range(len(paragraphs)))
    # 段落単位で後ろから（URL段落以外）削る
    removable = [i for i in range(len(paragraphs) - 1, -1, -1) if i not in url_idx_set]
    for i in removable:
        if twitter_weight(compose(keep)) <= target_weight:
            break
        keep.discard(i)

    result = compose(keep)
    if twitter_weight(result) <= target_weight:
        return result

    # それでもオーバーなら、行単位で末尾から削る（URL行以外）
    lines = result.split("\n")
    while twitter_weight("\n".join(lines)) > target_weight and len(lines) > 1:
        # URL 行が最終ならスキップして前の行を削る
        idx = len(lines) - 1
        while idx >= 0 and URL_PATTERN.search(lines[idx]):
            idx -= 1
        if idx < 0:
            break
        lines.pop(idx)

    final = "\n".join(lines).rstrip()
    return final


# --- キュー操作 ---


def load_queue() -> list[dict]:
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save_queue(entries: list[dict]) -> None:
    QUEUE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


# スロット → カテゴリ許可リスト
# キューエントリに category を持たせると、8/12/21時の各スロットで
# 該当カテゴリの post を優先選択する（マッチ無し時は最古 pending にフォールバック）。
# 連載枠: monthly_business_series (月-金朝) / dev_diary_series (月-金夕)
SLOT_CATEGORY_MAP: dict[str, list[str]] = {
    "morning_talk":  ["monthly_business_series", "morning_talk", "numbers"],  # 8時
    "lunch_light":   ["lunch_light", "howto"],                                # 12時
    "evening_chat":  ["dev_diary_series", "casual", "follower_chat"],         # 21時
    "default":       [],
}


def pick_next(entries: list[dict], slot: str | None = None) -> dict | None:
    """次に投稿するエントリを選択。

    slot が指定され、SLOT_CATEGORY_MAP に対応するカテゴリが定義されていれば、
    キューエントリの "category" フィールドに該当する pending を優先。
    マッチが無い場合は scheduled_at 最古の pending にフォールバック。
    """
    now = datetime.now()
    pending = [
        e for e in entries
        if e.get("status") == "pending"
        and datetime.fromisoformat(e["scheduled_at"]) <= now
    ]
    if not pending:
        return None

    if slot:
        allowed = SLOT_CATEGORY_MAP.get(slot, [])
        if allowed:
            allowed_set = set(allowed)
            slot_matches = [e for e in pending if e.get("category") in allowed_set]
            if slot_matches:
                slot_matches.sort(key=lambda e: e["scheduled_at"])
                log(f"slot={slot} matched {len(slot_matches)} entries by category")
                return slot_matches[0]
            log(f"slot={slot} no category match, falling back to oldest pending")

    pending.sort(key=lambda e: e["scheduled_at"])
    return pending[0]


# --- 人間風タイピング ---


async def human_type(page, text: str) -> None:
    for ch in text:
        if ch == "\n":
            await page.keyboard.press("Enter")
        else:
            await page.keyboard.type(ch)
        delay_ms = random.uniform(40, 180)
        if ch in "、。！？\n":
            delay_ms += random.uniform(200, 600)
        if random.random() < 0.05:
            delay_ms += random.uniform(500, 1500)
        await asyncio.sleep(delay_ms / 1000)


# --- 失敗タイプ ---


class FailureType:
    TEXTAREA_NOT_FOUND = "textarea_not_found"
    POST_BUTTON_NOT_FOUND = "post_button_not_found"
    POST_BUTTON_DISABLED = "post_button_disabled"  # 文字数オーバー等で非活性
    SESSION_EXPIRED = "session_expired"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


# --- 単発投稿（自己修復戦略対応）---


TEXTAREA_SELECTORS = [
    '[data-testid="tweetTextarea_0"]',
    'div[role="textbox"][aria-label*="Post text"]',
    'div[role="textbox"][aria-label*="ポスト本文"]',
    'div[role="textbox"][contenteditable="true"]',
]

POST_BUTTON_SELECTORS = [
    '[data-testid="tweetButton"]',
    '[data-testid="tweetButtonInline"]',
    'button[aria-label="Post"]',
    'button[aria-label="ポスト"]',
]


async def detect_failure_context(page) -> str | None:
    """現在のページから失敗タイプを推定。"""
    url = page.url or ""
    if "login" in url or "signin" in url or "flow/login" in url:
        return FailureType.SESSION_EXPIRED
    # ログインボタンが見えたらセッション切れ
    login_btn = await page.query_selector('a[href="/login"], a[data-testid="login"]')
    if login_btn:
        return FailureType.SESSION_EXPIRED
    return None


async def try_post_once(text: str, extra_wait_s: float = 0) -> tuple[bool, str, str | None]:
    """1 回試行。成功: (True, "", tweet_url), 失敗: (False, failure_type, err_msg)。"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=50)
        try:
            context = await browser.new_context(
                storage_state=str(SESSION),
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(4, 7) + extra_wait_s)

            # セッション確認
            session_err = await detect_failure_context(page)
            if session_err == FailureType.SESSION_EXPIRED:
                return False, FailureType.SESSION_EXPIRED, "home でセッション切れ検出"

            await page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=30000)

            # テキストエリア探索（fallback付き）
            textarea = None
            timeout_ms = int(15000 + extra_wait_s * 1000)
            for selector in TEXTAREA_SELECTORS:
                try:
                    textarea = await page.wait_for_selector(
                        selector, timeout=timeout_ms, state="visible"
                    )
                    if textarea:
                        break
                except PWTimeoutError:
                    continue

            if not textarea:
                await _save_screenshot(page, "textarea_not_found")
                return False, FailureType.TEXTAREA_NOT_FOUND, "全セレクタで未検出"

            # フォーカス
            box = await textarea.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + random.uniform(50, 200),
                    box["y"] + random.uniform(10, 40),
                    steps=random.randint(5, 12),
                )
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await page.mouse.click(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
            await asyncio.sleep(random.uniform(0.8, 1.5))

            # タイピング
            await human_type(page, text)
            await asyncio.sleep(random.uniform(2, 4))

            # Post ボタン（fallback付き）
            post_btn = None
            for selector in POST_BUTTON_SELECTORS:
                try:
                    post_btn = await page.wait_for_selector(
                        selector, timeout=5000, state="visible"
                    )
                    if post_btn:
                        break
                except PWTimeoutError:
                    continue

            if not post_btn:
                await _save_screenshot(page, "post_button_not_found")
                return False, FailureType.POST_BUTTON_NOT_FOUND, "全セレクタで未検出"

            # Post ボタンが disabled かチェック（文字数オーバー等）
            is_disabled = await post_btn.get_attribute("aria-disabled")
            is_disabled_attr = await post_btn.get_attribute("disabled")
            if is_disabled == "true" or is_disabled_attr is not None:
                await _save_screenshot(page, "post_button_disabled")
                return False, FailureType.POST_BUTTON_DISABLED, "ボタンが non-active（文字数等）"

            await asyncio.sleep(random.uniform(3, 6))
            await post_btn.click()

            # 結果確認
            try:
                await page.wait_for_url("**/home**", timeout=15000)
            except PWTimeoutError:
                # home に戻らなくても、投稿ダイアログが閉じてれば成功の可能性
                modal = await page.query_selector('[role="dialog"]')
                if modal:
                    await _save_screenshot(page, "post_clicked_but_dialog_remains")
                    return False, FailureType.UNKNOWN, "クリック後もダイアログ残留"

            await asyncio.sleep(random.uniform(3, 5))

            # プロフィールから URL 取得
            await page.goto("https://x.com/Rttvx2026", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(4, 6))

            first = await page.query_selector('article[data-testid="tweet"] a[href*="/status/"]')
            href = await first.get_attribute("href") if first else None
            url = f"https://x.com{href}" if href else None

            return True, "", url
        except PWTimeoutError as e:
            return False, FailureType.TIMEOUT, f"PWTimeout: {e}"
        except Exception as e:
            return False, FailureType.UNKNOWN, f"{type(e).__name__}: {e}"
        finally:
            await browser.close()


async def _save_screenshot(page, tag: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        await page.screenshot(path=str(SCREENSHOT_DIR / f"x_fail_{tag}_{stamp}.png"))
    except Exception:
        pass


# --- 自己修復本体 ---


async def post_with_self_healing(entry: dict) -> tuple[bool, str | None, dict]:
    """1 エントリを自己修復型で投稿。(success, tweet_url, meta)。"""
    original_text = entry["text"]
    current_text = original_text
    meta = {"attempts": [], "fixes_applied": []}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"  [attempt {attempt}/{MAX_ATTEMPTS}] weight={twitter_weight(current_text)} chars={len(current_text)}")

        # 事前チェック: 文字数オーバーは事前に短縮
        if twitter_weight(current_text) > TWEET_LIMIT:
            shrunk = shrink_text(current_text)
            log(f"    [pre-fix] 文字数オーバー → 自動短縮 {twitter_weight(current_text)}→{twitter_weight(shrunk)}")
            current_text = shrunk
            meta["fixes_applied"].append(f"shrink_pre_attempt_{attempt}")

        extra_wait = 0 if attempt == 1 else (attempt - 1) * 5.0

        success, failure_type, err = await try_post_once(current_text, extra_wait_s=extra_wait)
        meta["attempts"].append({
            "n": attempt,
            "success": success,
            "failure_type": failure_type,
            "error": err,
            "text_used_chars": len(current_text),
            "text_used_weight": twitter_weight(current_text),
        })

        if success:
            log(f"  [OK] attempt={attempt} url={err if success else ''}")
            return True, err, meta  # err here holds tweet_url on success (return value)

        log(f"  [FAIL] attempt={attempt} type={failure_type} err={err}")

        # 修復戦略
        if failure_type == FailureType.SESSION_EXPIRED:
            notify(
                "🔑 X セッション切れ、投稿失敗\n"
                f"entry_id={entry['id']}\n"
                "scripts/x_auth_init.sh または Cookie 再取得を実行してください"
            )
            meta["fixes_applied"].append("session_expired_notify")
            return False, None, meta

        if failure_type == FailureType.POST_BUTTON_DISABLED:
            # 強制短縮してもう一度（段落削減）
            shrunk = shrink_text(current_text, target_weight=TWEET_LIMIT - 20)
            if shrunk != current_text and twitter_weight(shrunk) < twitter_weight(current_text):
                current_text = shrunk
                meta["fixes_applied"].append(f"shrink_after_disabled_attempt_{attempt}")
                log(f"    [fix] 強制短縮 新weight={twitter_weight(current_text)}")
                continue
            else:
                log("    [fix] これ以上短縮できず、断念")
                break

        if failure_type in (FailureType.TEXTAREA_NOT_FOUND, FailureType.POST_BUTTON_NOT_FOUND, FailureType.TIMEOUT, FailureType.UNKNOWN):
            # 待機時間延長 + 再試行（extra_wait は次ループで自動増加）
            meta["fixes_applied"].append(f"extra_wait_attempt_{attempt + 1}")
            # 次ループの前にクールダウン
            backoff = min(30 * attempt, 120)
            log(f"    [fix] {backoff}秒クールダウン後に再試行")
            await asyncio.sleep(backoff)
            continue

    # 全attempt失敗
    notify(
        f"❌ X 投稿失敗（{MAX_ATTEMPTS}回リトライ後）\n"
        f"entry_id={entry['id']}\n"
        f"最終 failure_type: {meta['attempts'][-1]['failure_type'] if meta['attempts'] else 'n/a'}"
    )
    return False, None, meta


async def main() -> int:
    parser = argparse.ArgumentParser(description="X 単発投稿（キューから1件、スロット対応）")
    parser.add_argument(
        "--slot",
        choices=list(SLOT_CATEGORY_MAP.keys()),
        default=None,
        help="時間帯スロット (morning_news/morning_talk/lunch_light/day_recap/evening_chat/default)",
    )
    args = parser.parse_args()
    slot = args.slot

    if not SESSION.exists():
        log(f"FATAL: {SESSION} なし")
        return 1

    entries = load_queue()
    entry = pick_next(entries, slot=slot)

    if not entry:
        log(f"no pending posts (slot={slot})")
        return 0

    log(
        f"posting id={entry['id']}, scheduled_at={entry['scheduled_at']}, "
        f"slot={slot}, category={entry.get('category')}"
    )

    success, tweet_url, meta = await post_with_self_healing(entry)

    if success:
        entry["status"] = "posted"
        entry["posted_at"] = datetime.now().isoformat(timespec="seconds")
        entry["tweet_url"] = tweet_url
        entry["healing_meta"] = meta
        save_queue(entries)
        log(f"OK: id={entry['id']} url={tweet_url} fixes={meta['fixes_applied']}")
        return 0
    else:
        entry["attempts"] = entry.get("attempts", 0) + len(meta["attempts"])
        entry["status"] = "failed"
        entry["last_error"] = meta["attempts"][-1]["error"] if meta["attempts"] else "n/a"
        entry["healing_meta"] = meta
        save_queue(entries)
        log(f"ERROR: id={entry['id']} status=failed fixes={meta['fixes_applied']}")
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
