"""リプライメトリクス更新 — reply_log.jsonl を使った2段階処理。

ステップ1: reply_tweet_id 推定
  reply_log.jsonl に reply_tweet_id が null の行がある。
  自分のプロフィール最新リプ (https://x.com/<handle>/with_replies) から
  「target_tweet_id への返信」を target で突合して reply_tweet_id を推定する。

ステップ2: analytics 取得
  reply_tweet_id が確定したものに対して analytics ページから
  impressions / likes / engagement_rate / posted_at を取る。

出力:
  - logs/reply_log_metrics.jsonl     1 行 1 スナップショット (append)
  - Supabase REST upsert             table: x_reply_metrics
                                     ON CONFLICT (reply_id, captured_at)
                                     env が無ければ JSONL のみ (fail-open)

入力:
  - reply_log.jsonl
    デフォルト: ${REPLY_LOG_PATH:-/Users/apple/NorthValueAsset/content/x-viral-reply/reply_log.jsonl}

使い方:
  python -m scripts.reply_metrics_updater --recent 3
  python -m scripts.reply_metrics_updater --dry-run

cron: 1 時間毎想定 (post collector と 15 分オフセット)。
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
JSONL_OUT = LOG_DIR / "reply_log_metrics.jsonl"
HANDLE = os.getenv("X_HANDLE", "Rttvx2026")

REPLY_LOG_PATH = Path(
    os.getenv(
        "REPLY_LOG_PATH",
        "/Users/apple/NorthValueAsset/content/x-viral-reply/reply_log.jsonl",
    )
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

ANALYTICS_RATE_LIMIT_SEC = 5.0
SUPABASE_TABLE = "x_reply_metrics"


# ---------------------------------------------------------------------------
# 共通ユーティリティ (post collector と重複だが循環import回避のため転記)
# ---------------------------------------------------------------------------

def parse_count(text: str | None) -> int:
    if not text:
        return 0
    t = text.strip().replace(",", "").replace(" ", "").replace(" ", "")
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
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*%", text)
    return float(m.group(1)) if m else None


def _within_recent_days(ts: str | None, days: int) -> bool:
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


# ---------------------------------------------------------------------------
# reply_log.jsonl 読み込み
# ---------------------------------------------------------------------------

def load_reply_log(recent_days: int) -> list[dict[str, Any]]:
    if not REPLY_LOG_PATH.exists():
        print(f"WARN: reply log not found: {REPLY_LOG_PATH}", file=sys.stderr)
        return []
    rows: list[dict[str, Any]] = []
    with REPLY_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not _within_recent_days(row.get("timestamp"), recent_days):
                continue
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# reply_tweet_id 推定 — 自分の with_replies を見て target_tweet_id 突合
# ---------------------------------------------------------------------------

WITH_REPLIES_URL = f"https://x.com/{HANDLE}/with_replies"
STATUS_LINK_RE = re.compile(rf"/{re.escape(HANDLE)}/status/(\d+)")


async def collect_my_recent_replies(page) -> list[dict[str, Any]]:
    """with_replies ページを開いて、自分の reply とその返信先 target を集める。

    返り値: [{reply_id, target_tweet_id, target_user}, ...]
    """
    await page.goto(WITH_REPLIES_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for _ in range(8):
        # tweet article 単位で見る
        articles = await page.query_selector_all('article[data-testid="tweet"]')
        for art in articles:
            try:
                html = await art.inner_html()
            except Exception:
                continue
            # 自分の status link を拾う (= reply id)
            m_self = STATUS_LINK_RE.search(html)
            if not m_self:
                continue
            reply_id = m_self.group(1)
            if reply_id in seen_ids:
                continue
            # 返信先 (Replying to ...) は ancestor の status link を持っているケースが多い。
            # シンプル化: 同じHTML内の他 status リンクで自分以外の id の最初のものを target にする。
            target_id = None
            target_user = None
            for m in re.finditer(r"/([A-Za-z0-9_]+)/status/(\d+)", html):
                if m.group(1) == HANDLE and m.group(2) == reply_id:
                    continue
                if m.group(2) != reply_id:
                    target_id = m.group(2)
                    target_user = m.group(1)
                    break
            if target_id:
                out.append(
                    {
                        "reply_id": reply_id,
                        "target_tweet_id": target_id,
                        "target_user": f"@{target_user}",
                    }
                )
                seen_ids.add(reply_id)

        await page.mouse.wheel(0, 4000)
        await asyncio.sleep(2)

    return out


# ---------------------------------------------------------------------------
# analytics 取得 (リプ用)
# ---------------------------------------------------------------------------

async def fetch_reply_analytics(page, reply_id: str) -> dict[str, Any] | None:
    url = f"https://x.com/{HANDLE}/status/{reply_id}/analytics"
    for attempt in (1, 2):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)
            body = await page.inner_text("body")
            if not body:
                if attempt == 2:
                    return None
                continue
            impressions = _label_num(body, [r"Impressions", r"インプレッション"])
            likes = _label_num(body, [r"Likes", r"いいね"])
            er = _label_pct(body, [r"Engagement rate", r"エンゲージメント率"])
            # posted_at は status ページ側に行かないと取りにくい。analytics ページにも time タグがあれば取る。
            posted_at = None
            try:
                el = await page.query_selector('time[datetime]')
                if el:
                    posted_at = await el.get_attribute("datetime")
            except Exception:
                pass
            return {
                "impressions": impressions,
                "likes": likes,
                "engagement_rate": er,
                "posted_at": posted_at,
            }
        except Exception as exc:
            if attempt == 2:
                print(f"WARN: reply analytics failed reply_id={reply_id}: {exc}", file=sys.stderr)
                return None
    return None


def _label_num(text: str, labels: list[str]) -> int:
    for label in labels:
        m = re.search(rf"{label}[\s　:：]*\n?\s*([\d,.\sKM万]+)", text)
        if m:
            return parse_count(m.group(1))
    return 0


def _label_pct(text: str, labels: list[str]) -> float | None:
    for label in labels:
        m = re.search(rf"{label}[\s　:：]*\n?\s*([\d.]+\s*%)", text)
        if m:
            return parse_engagement_rate(m.group(1))
    return None


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def append_jsonl(entry: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL_OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def supabase_upsert(rows: Iterable[dict[str, Any]]) -> tuple[int, str | None]:
    rows = list(rows)
    if not rows:
        return 0, "empty"
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return 0, "no-supabase"
    endpoint = f"{url.rstrip('/')}/rest/v1/{SUPABASE_TABLE}?on_conflict=reply_id,captured_at"
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
# メイン処理
# ---------------------------------------------------------------------------

async def run(recent_days: int, dry_run: bool) -> int:
    log_rows = load_reply_log(recent_days)
    print(f"INFO: reply_log entries (recent {recent_days}d) = {len(log_rows)}")

    if dry_run:
        missing_ids = [r for r in log_rows if not r.get("reply_tweet_id")]
        print(f"[DRY-RUN] entries needing reply_id resolution: {len(missing_ids)}")
        print(f"[DRY-RUN] entries already with reply_id:        {len(log_rows) - len(missing_ids)}")
        print(f"[DRY-RUN] reply_log path: {REPLY_LOG_PATH}")
        print(f"[DRY-RUN] output -> {JSONL_OUT}")
        print(f"[DRY-RUN] supabase -> {'enabled' if os.getenv('SUPABASE_URL') and os.getenv('SUPABASE_SERVICE_ROLE_KEY') else 'disabled (env missing)'}")
        return 0

    if not SESSION_FILE.exists():
        print(f"ERROR: session file not found: {SESSION_FILE}", file=sys.stderr)
        return 1

    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output_rows: list[dict[str, Any]] = []

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

            # 1) 自分の最近のリプを収集 (target で突合するため)
            need_resolve = [r for r in log_rows if not r.get("reply_tweet_id") and r.get("target_tweet_id")]
            recent_my_replies: list[dict[str, Any]] = []
            if need_resolve:
                recent_my_replies = await collect_my_recent_replies(page)
                print(f"INFO: collected {len(recent_my_replies)} replies from with_replies page")

            target_to_reply: dict[str, str] = {
                r["target_tweet_id"]: r["reply_id"] for r in recent_my_replies
            }

            # 2) reply_id 確定 + analytics 取得
            for entry in log_rows:
                reply_id = entry.get("reply_tweet_id")
                resolved_via_match = False
                if not reply_id:
                    target_id = entry.get("target_tweet_id")
                    if target_id and target_id in target_to_reply:
                        reply_id = target_to_reply[target_id]
                        resolved_via_match = True
                if not reply_id:
                    # 解決できないものはスキップだが、ステータス行は残す
                    continue

                await asyncio.sleep(ANALYTICS_RATE_LIMIT_SEC)
                metrics = await fetch_reply_analytics(page, reply_id)
                if metrics is None:
                    continue

                row = {
                    "reply_id": reply_id,
                    "target_tweet_id": entry.get("target_tweet_id"),
                    "target_user": entry.get("target_user"),
                    "reply_text": entry.get("reply_text"),
                    "captured_at": captured_at,
                    "resolved_via_match": resolved_via_match,
                    **metrics,
                }
                append_jsonl(row)
                output_rows.append(row)
                await asyncio.sleep(ANALYTICS_RATE_LIMIT_SEC)
        finally:
            await browser.close()

    status, err = supabase_upsert(output_rows)
    if err == "no-supabase":
        print(f"OK: jsonl-only ({len(output_rows)} rows). Supabase env not set.")
    elif err:
        print(f"WARN: supabase upsert failed status={status} err={err}", file=sys.stderr)
    else:
        print(f"OK: jsonl + supabase upsert ({len(output_rows)} rows, status={status})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="X reply analytics updater")
    ap.add_argument("--recent", type=int, default=3, help="直近 N 日 (default: 3)")
    ap.add_argument("--dry-run", action="store_true", help="ネット接続なしで構成のみ確認")
    args = ap.parse_args()
    return asyncio.run(run(args.recent, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
