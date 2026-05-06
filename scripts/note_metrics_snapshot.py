#!/usr/bin/env python3
"""note 記事メトリクス（閲覧数・いいね数・コメント数）を取得して JSON に保存する。

公開 API には閲覧数が無いため、ログイン済 .note-session.json を使ってダッシュボード API から取得する。
失敗時は公開 API のいいね/コメントだけのスナップショットにフォールバック。

Usage:
    .venv/bin/python scripts/note_metrics_snapshot.py
    .venv/bin/python scripts/note_metrics_snapshot.py --no-auth   # 公開APIのみ
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_PATH = BASE_DIR / ".note-session.json"
OUTPUT_PATH = BASE_DIR / "logs" / "note_metrics_snapshot.json"

CREATOR_ID = "kaitori_nv_cloud"
PUBLIC_API = f"https://note.com/api/v2/creators/{CREATOR_ID}/contents?kind=note&page="
DASHBOARD_STATS_API = "/api/v1/stats/pv?filter=all&page={page}&sort=pv"


def fetch_public(client: httpx.Client) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 30):
        r = client.get(PUBLIC_API + str(page))
        d = r.json()
        items = d.get("data", {}).get("contents", [])
        if not items:
            break
        for it in items:
            out.append({
                "id": it.get("id"),
                "key": it.get("key"),
                "publishAt": it.get("publishAt", ""),
                "title": it.get("name", ""),
                "url": it.get("noteUrl", ""),
                "likes": it.get("likeCount", 0),
                "comments": it.get("commentCount", 0),
                "tags": [h.get("hashtag", {}).get("name", "") for h in it.get("hashtags", [])],
            })
        if d.get("data", {}).get("isLastPage"):
            break
        time.sleep(0.4)
    return out


async def fetch_views_via_session() -> tuple[dict[int, int], dict]:
    """ログイン済セッションを使ってダッシュボード API から PV を取得。

    Returns:
        ({note_id: pv_count}, totals_dict). 失敗時は ({}, {}).
    """
    if not SESSION_PATH.exists():
        return {}, {}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("⚠ playwright未インストール → 閲覧数取得スキップ", file=sys.stderr)
        return {}, {}

    pv_map: dict[int, int] = {}
    totals: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(SESSION_PATH))
        page = await context.new_page()
        try:
            await page.goto("https://note.com/dashboard", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            if "/login" in page.url:
                print("⚠ セッション切れ (/login へリダイレクト)", file=sys.stderr)
                return {}, {}

            for page_no in range(1, 30):
                resp = await page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {credentials: 'include', headers: {'accept': 'application/json'}});
                        return {status: r.status, body: await r.text()};
                    }""",
                    DASHBOARD_STATS_API.format(page=page_no),
                )
                if resp.get("status") != 200:
                    break
                try:
                    data = json.loads(resp["body"]).get("data", {})
                except Exception:
                    break
                notes = data.get("note_stats", [])
                if not notes:
                    break
                for n in notes:
                    nid = n.get("id")
                    pv = n.get("read_count")
                    if nid is not None and pv is not None:
                        pv_map[int(nid)] = int(pv)
                if not totals:
                    totals = {
                        "total_pv": data.get("total_pv", 0),
                        "total_like": data.get("total_like", 0),
                        "total_comment": data.get("total_comment", 0),
                        "last_calculate_at": data.get("last_calculate_at", ""),
                    }
                if data.get("last_page"):
                    break
                await page.wait_for_timeout(300)
        finally:
            await context.close()
            await browser.close()
    return pv_map, totals


def summarize(rows: list[dict]) -> None:
    n = len(rows)
    if n == 0:
        print("記事ゼロ")
        return
    total_likes = sum(r["likes"] for r in rows)
    total_comments = sum(r["comments"] for r in rows)
    total_views = sum(r.get("views", 0) for r in rows)
    has_views = any(r.get("views") for r in rows)

    print(f"取得記事数: {n}")
    print(f"合計いいね: {total_likes}, 合計コメント: {total_comments}", end="")
    if has_views:
        print(f", 合計閲覧: {total_views}")
    else:
        print()
    print(f"いいね0件: {sum(1 for r in rows if r['likes'] == 0)}/{n}")
    print()

    print("=== TOP 10 (いいね) ===")
    for r in sorted(rows, key=lambda x: x["likes"], reverse=True)[:10]:
        d = r["publishAt"][:10]
        v = f" {r['views']:>4}👁" if r.get("views") else ""
        print(f"  {d} {r['likes']:>3}♡ {r['comments']}💬{v} | {r['title'][:50]}")

    if has_views:
        print()
        print("=== TOP 10 (閲覧数) ===")
        for r in sorted(rows, key=lambda x: x.get("views", 0), reverse=True)[:10]:
            d = r["publishAt"][:10]
            print(f"  {d} {r.get('views', 0):>4}👁 {r['likes']}♡ | {r['title'][:50]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-auth", action="store_true", help="公開APIのみ（閲覧数なし）")
    parser.add_argument("--quiet", action="store_true", help="サマリ出力なし")
    args = parser.parse_args()

    with httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as c:
        rows = fetch_public(c)

    totals: dict = {}
    if not args.no_auth:
        try:
            pv_map, totals = asyncio.run(fetch_views_via_session())
        except Exception as e:
            print(f"⚠ 閲覧数取得に失敗: {e}", file=sys.stderr)
            pv_map = {}
        for r in rows:
            v = pv_map.get(r["id"]) if r.get("id") else None
            if v is not None:
                r["views"] = v
    if totals and not args.quiet:
        print(f"[totals] PV={totals.get('total_pv')} Like={totals.get('total_like')} Comment={totals.get('total_comment')} (集計: {totals.get('last_calculate_at')})")
        print()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        summarize(rows)
        print(f"\n保存: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
