"""X 投稿メトリクス収集 — analytics ページから 1 投稿ずつ取得する。

対象: 直近 N 日以内の自分の投稿
出力:
  - logs/x_post_metrics.jsonl   1 行 1 スナップショット (append)
  - Supabase REST upsert        env (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY) が
                                揃っていれば table: x_post_metrics に upsert
                                ON CONFLICT (post_id, captured_at)
                                揃っていなければ JSONL のみ書き出し (fail-open)

使い方:
  python -m scripts.x_post_metrics_collector --recent 7
  python -m scripts.x_post_metrics_collector --post-id 1234567890
  python -m scripts.x_post_metrics_collector --dry-run

cron: 1 時間毎想定。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_FILE = BASE_DIR / ".x-session.json"
LOG_DIR = BASE_DIR / "logs"
JSONL_OUT = LOG_DIR / "x_post_metrics.jsonl"
HANDLE = os.getenv("X_HANDLE", "Rttvx2026")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

ANALYTICS_RATE_LIMIT_SEC = 5.0
PROFILE_LOAD_TIMEOUT_MS = 30000
ANALYTICS_LOAD_TIMEOUT_MS = 30000
SUPABASE_TABLE = "x_post_metrics"


# ---------------------------------------------------------------------------
# 数値パース
# ---------------------------------------------------------------------------

def parse_count(text: str | None) -> int:
    if not text:
        return 0
    t = text.strip().replace(",", "").replace(" ", "").replace(" ", "")
    m_k = re.match(r"^([\d.]+)K$", t, re.I)
    m_m = re.match(r"^([\d.]+)M$", t, re.I)
    m_man = re.match(r"^([\d.]+)万$", t)
    if m_k:
        return int(float(m_k.group(1)) * 1000)
    if m_m:
        return int(float(m_m.group(1)) * 1_000_000)
    if m_man:
        return int(float(m_man.group(1)) * 10000)
    digits = "".join(c for c in t if c.isdigit())
    return int(digits) if digits else 0


def parse_engagement_rate(text: str | None) -> float | None:
    """'2.4%' や '0.5%' を float (パーセント) で返す。取得不可なら None。"""
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*%", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# プロフィールから直近 N 日の post_id 列挙
# ---------------------------------------------------------------------------

POST_ID_RE = re.compile(rf"/{re.escape(HANDLE)}/status/(\d+)")


async def list_recent_post_ids(page, days: int) -> list[str]:
    """プロフィールページに行き、見えている投稿の post_id を集める。"""
    await page.goto(f"https://x.com/{HANDLE}", wait_until="domcontentloaded", timeout=PROFILE_LOAD_TIMEOUT_MS)
    await asyncio.sleep(4)

    seen: list[str] = []
    seen_set: set[str] = set()
    # ざっくり 5 回スクロール (最近の数日分が拾えれば十分)
    for _ in range(5):
        html = await page.content()
        for m in POST_ID_RE.finditer(html):
            pid = m.group(1)
            if pid not in seen_set:
                seen_set.add(pid)
                seen.append(pid)
        await page.mouse.wheel(0, 4000)
        await asyncio.sleep(2)

    # date filter は posted_at が取れた段階で適用する。ここでは pid だけ返す。
    return seen


# ---------------------------------------------------------------------------
# analytics ページから値を取得
# ---------------------------------------------------------------------------

# analytics ページのHTML本文から、ラベル → 数値 の組を強引に拾う。
# 並びはYYYY-MM時点のUI例: Impressions / Engagements / Detail expands /
#   Profile visits / New followers / Likes / Reposts / Replies / Bookmarks /
#   Quote posts / Profile visits / Engagement rate
LABEL_PATTERNS: dict[str, list[str]] = {
    "impressions": [r"Impressions", r"インプレッション"],
    "likes":       [r"Likes", r"いいね"],
    "reposts":     [r"Reposts", r"リポスト"],
    "quotes":      [r"Quote posts", r"Quotes", r"引用"],
    "replies":     [r"Replies", r"返信"],
}


def _extract_number_after_label(text: str, label_patterns: list[str]) -> int:
    """テキスト本文から「ラベル … 数値」のペアを最も近い順に探す。"""
    for label in label_patterns:
        # ラベルの直後 (改行/空白を挟んで) の最初の数字列
        m = re.search(rf"{label}[\s　:：]*\n?\s*([\d,.\sKM万]+)", text)
        if m:
            val = parse_count(m.group(1))
            if val or label_patterns[0] in ("Replies", "Quote posts"):
                return val
    return 0


def _extract_engagement_rate(text: str) -> float | None:
    for label in (r"Engagement rate", r"エンゲージメント率"):
        m = re.search(rf"{label}[\s　:：]*\n?\s*([\d.]+\s*%)", text)
        if m:
            return parse_engagement_rate(m.group(1))
    return None


async def fetch_post_text_and_time(page, post_id: str) -> tuple[str | None, str | None]:
    """status ページから本文と投稿時刻を抜く。"""
    try:
        await page.goto(
            f"https://x.com/{HANDLE}/status/{post_id}",
            wait_until="domcontentloaded",
            timeout=ANALYTICS_LOAD_TIMEOUT_MS,
        )
        await asyncio.sleep(3)
        # 本文
        try:
            text_el = await page.query_selector('[data-testid="tweetText"]')
            text = await text_el.inner_text() if text_el else None
        except Exception:
            text = None
        # 投稿時刻 (time タグの datetime 属性)
        try:
            time_el = await page.query_selector('time[datetime]')
            posted_at = await time_el.get_attribute("datetime") if time_el else None
        except Exception:
            posted_at = None
        return text, posted_at
    except Exception:
        return None, None


async def fetch_analytics(page, post_id: str) -> dict[str, Any] | None:
    """analytics ページから数値だけ抜き、辞書で返す。失敗時は None。"""
    url = f"https://x.com/{HANDLE}/status/{post_id}/analytics"
    for attempt in (1, 2):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=ANALYTICS_LOAD_TIMEOUT_MS)
            await asyncio.sleep(4)
            body = await page.inner_text("body")
            if not body or "Analytics" not in body and "アナリティクス" not in body:
                if attempt == 2:
                    return None
                await asyncio.sleep(3)
                continue
            return {
                "impressions": _extract_number_after_label(body, LABEL_PATTERNS["impressions"]),
                "likes":       _extract_number_after_label(body, LABEL_PATTERNS["likes"]),
                "reposts":     _extract_number_after_label(body, LABEL_PATTERNS["reposts"]),
                "quotes":      _extract_number_after_label(body, LABEL_PATTERNS["quotes"]),
                "replies":     _extract_number_after_label(body, LABEL_PATTERNS["replies"]),
                "engagement_rate": _extract_engagement_rate(body),
            }
        except Exception as exc:
            if attempt == 2:
                print(f"WARN: analytics fetch failed post_id={post_id}: {exc}", file=sys.stderr)
                return None
            await asyncio.sleep(3)
    return None


# ---------------------------------------------------------------------------
# 出力 (JSONL + Supabase)
# ---------------------------------------------------------------------------

def append_jsonl(entry: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL_OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def supabase_upsert(rows: Iterable[dict[str, Any]]) -> tuple[int, str | None]:
    """Supabase REST で upsert。戻り値: (status_code, error_text or None)。

    env が無ければ (0, "no-supabase") を返して呼び出し側で fail-open する。
    """
    rows = list(rows)
    if not rows:
        return 0, "empty"
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return 0, "no-supabase"
    endpoint = f"{url.rstrip('/')}/rest/v1/{SUPABASE_TABLE}?on_conflict=post_id,captured_at"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        r = requests.post(endpoint, headers=headers, json=rows, timeout=20)
        if r.status_code >= 400:
            return r.status_code, r.text[:300]
        return r.status_code, None
    except requests.RequestException as exc:
        return 0, f"request-error: {exc}"


# ---------------------------------------------------------------------------
# 日付フィルタ
# ---------------------------------------------------------------------------

def _within_recent_days(posted_at: str | None, days: int) -> bool:
    if not posted_at:
        # 取れなかったら念のため対象に残す (cron が重複してもJSONLは追記なので無害)
        return True
    try:
        # 例: 2026-04-30T10:23:51.000Z
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

async def run(post_ids: list[str], dry_run: bool, recent_days: int) -> int:
    if dry_run:
        print(f"[DRY-RUN] would fetch analytics for {len(post_ids)} post(s): {post_ids[:5]}...")
        print(f"[DRY-RUN] output -> {JSONL_OUT}")
        print(f"[DRY-RUN] supabase -> {'enabled' if os.getenv('SUPABASE_URL') and os.getenv('SUPABASE_SERVICE_ROLE_KEY') else 'disabled (env missing)'}")
        return 0

    if not SESSION_FILE.exists():
        print(f"ERROR: session file not found: {SESSION_FILE}", file=sys.stderr)
        return 1

    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    ok_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                storage_state=str(SESSION_FILE),
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            # post_ids が未指定なら、プロフィールから直近 N 日分を列挙
            if not post_ids:
                post_ids = await list_recent_post_ids(page, recent_days)
                print(f"INFO: collected {len(post_ids)} candidate post(s) from profile")

            for pid in post_ids:
                text, posted_at = await fetch_post_text_and_time(page, pid)
                if not _within_recent_days(posted_at, recent_days):
                    continue
                await asyncio.sleep(ANALYTICS_RATE_LIMIT_SEC)
                metrics = await fetch_analytics(page, pid)
                if metrics is None:
                    print(f"WARN: skipped post_id={pid} (analytics unreachable)", file=sys.stderr)
                    continue

                row = {
                    "post_id": pid,
                    "captured_at": captured_at,
                    "posted_at": posted_at,
                    "text": text,
                    **metrics,
                }
                append_jsonl(row)
                rows.append(row)
                ok_count += 1
                await asyncio.sleep(ANALYTICS_RATE_LIMIT_SEC)
        finally:
            await browser.close()

    status, err = supabase_upsert(rows)
    if err == "no-supabase":
        print(f"OK: jsonl-only ({ok_count} rows). Supabase env not set.")
    elif err:
        print(f"WARN: supabase upsert failed status={status} err={err}", file=sys.stderr)
    else:
        print(f"OK: jsonl + supabase upsert ({ok_count} rows, status={status})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="X post analytics collector")
    ap.add_argument("--post-id", help="単一 post_id のみ処理")
    ap.add_argument("--recent", type=int, default=7, help="直近 N 日 (default: 7)")
    ap.add_argument("--dry-run", action="store_true", help="ネット接続なしで構成のみ確認")
    args = ap.parse_args()

    post_ids = [args.post_id] if args.post_id else []
    return asyncio.run(run(post_ids, args.dry_run, args.recent))


if __name__ == "__main__":
    sys.exit(main())
