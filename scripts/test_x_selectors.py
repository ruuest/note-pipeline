"""X(Twitter) 主要セレクタの存在確認ツール（誤投稿禁止版）。

x_publisher.py が使う全セレクタ配列を 2026-04 時点の X UI に対して
「存在するか／しないか」だけチェックする検証スクリプト。

- 実投稿は絶対にしない（クリック禁止、テキスト入力すらしない）
- home ページ + compose モーダル 両方のセレクタをチェック
- 見つからないカテゴリがあれば DOM ダンプから候補を列挙

使い方:
    # .x-session.json or ~/.x-playwright-profile で要ログイン状態
    python3 scripts/test_x_selectors.py
    python3 scripts/test_x_selectors.py --headless       # サーバ上で回す時
    python3 scripts/test_x_selectors.py --dump-dom       # 見つからない時に DOM 候補を出す

終了コード:
    0 全セレクタOK
    1 1つ以上のカテゴリで全滅（UI 変更の可能性）
    2 ログイン切れ
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from playwright.async_api import async_playwright  # noqa: E402

from src import x_publisher  # noqa: E402

PROFILE_DIR = Path.home() / ".x-playwright-profile"
SESSION_PATH = BASE_DIR / ".x-session.json"

# x_publisher の公開セレクタ配列をそのまま使う
CATEGORIES: dict[str, list[str]] = {
    "compose_open (サイドバー投稿ボタン)": x_publisher.XPublisher.COMPOSE_OPEN_SELECTORS,
    "tweet_textarea (本文入力欄)": x_publisher.XPublisher.TWEET_TEXTAREA_SELECTORS,
    "add_slot (+ 追加ボタン)": x_publisher.XPublisher.ADD_SLOT_SELECTORS,
    "post_all (送信ボタン)": x_publisher.XPublisher.POST_ALL_SELECTORS,
    "tweet_article (TL ツイート要素)": x_publisher.XPublisher.TWEET_ARTICLE_SELECTORS,
    "image_upload (画像アップロード)": x_publisher.XPublisher.IMAGE_UPLOAD_SELECTORS,
    "modal_close (モーダル閉じる)": x_publisher.XPublisher.MODAL_CLOSE_SELECTORS,
}

# home で確認するカテゴリ / compose 画面で確認するカテゴリ
HOME_CATEGORIES = {
    "compose_open (サイドバー投稿ボタン)",
    "tweet_article (TL ツイート要素)",
}
COMPOSE_CATEGORIES = {
    "tweet_textarea (本文入力欄)",
    "add_slot (+ 追加ボタン)",
    "post_all (送信ボタン)",
    "image_upload (画像アップロード)",
    "modal_close (モーダル閉じる)",
}


async def _check(page, selectors: list[str]) -> list[tuple[str, int]]:
    """各セレクタのヒット数を返す。クリックは絶対にしない。"""
    results: list[tuple[str, int]] = []
    for sel in selectors:
        try:
            cnt = await page.locator(sel).count()
        except Exception:
            cnt = -1
        results.append((sel, cnt))
    return results


async def _dump_dom_candidates(page) -> None:
    """data-testid / aria-label を網羅ダンプして UI 変更時の候補探しを助ける。"""
    js = """
    () => {
        const testids = new Set();
        const arias = new Set();
        document.querySelectorAll('[data-testid]').forEach(el => {
            testids.add(el.getAttribute('data-testid'));
        });
        document.querySelectorAll('[aria-label]').forEach(el => {
            arias.add(el.getAttribute('aria-label'));
        });
        return {
            testids: Array.from(testids).sort(),
            arias: Array.from(arias).sort(),
        };
    }
    """
    try:
        data: dict[str, list[str]] = await page.evaluate(js)
    except Exception as e:
        print(f"  DOM dump 失敗: {e}")
        return
    print("\n--- data-testid 一覧 ---")
    for tid in data.get("testids", []):
        print(f"  [data-testid=\"{tid}\"]")
    print("\n--- aria-label 一覧 ---")
    for al in data.get("arias", []):
        print(f"  [aria-label=\"{al}\"]")


def _summarize(page_name: str, category: str, results: list[tuple[str, int]]) -> tuple[bool, int]:
    """結果を出力し、(category_ok, hit_count) を返す。"""
    hits = [(s, c) for s, c in results if c > 0]
    misses = [(s, c) for s, c in results if c == 0]
    errs = [(s, c) for s, c in results if c < 0]
    ok = len(hits) > 0
    status = "✅" if ok else "❌"
    print(f"\n[{page_name}] {status} {category} — hit={len(hits)}/{len(results)}")
    for s, c in hits:
        print(f"    ✓ ({c:>3}件) {s}")
    for s, _ in misses:
        print(f"    × (  0件) {s}")
    for s, _ in errs:
        print(f"    ! (エラー) {s}")
    return ok, len(hits)


async def _run(headless: bool, dump_dom: bool) -> int:
    print("=" * 70)
    print("  X UI セレクタ存在確認 (実投稿禁止・クリック禁止)")
    print("=" * 70)
    print(f"Profile: {PROFILE_DIR}")
    print(f"Session: {SESSION_PATH} (exists={SESSION_PATH.exists()})")

    async with async_playwright() as p:
        # launch_persistent_context でログイン済み状態を再現
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=headless,
                user_agent=x_publisher.DEFAULT_USER_AGENT,
                viewport=x_publisher.DEFAULT_VIEWPORT,
                locale=x_publisher.DEFAULT_LOCALE,
                timezone_id=x_publisher.DEFAULT_TIMEZONE,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            print(f"❌ 実機 Chrome 起動失敗: {e}")
            print("   Google Chrome をインストール or scripts/x_auth_init.sh でプロファイル生成してください")
            return 1

        try:
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});"
                "Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});"
            )
            page = context.pages[0] if context.pages else await context.new_page()

            # ─── 1) /home ───
            print("\n[1/2] /home に遷移中...")
            try:
                await page.goto(x_publisher.X_PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"❌ /home 遷移失敗: {e}")
                return 2
            await page.wait_for_timeout(3500)

            if "/login" in page.url or "/i/flow/login" in page.url:
                print(f"❌ 未ログイン状態（URL={page.url}）")
                print("   scripts/x_auth_init.sh を実行してログインしてください")
                return 2

            category_ok: dict[str, bool] = {}
            for cat, sels in CATEGORIES.items():
                if cat not in HOME_CATEGORIES:
                    continue
                results = await _check(page, sels)
                ok, _ = _summarize("home", cat, results)
                category_ok[cat] = ok

            # ─── 2) /compose/post ───
            print("\n[2/2] /compose/post に遷移中...")
            try:
                await page.goto(x_publisher.X_COMPOSE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"❌ /compose/post 遷移失敗: {e}")
                return 2
            # モーダル描画待ち
            await page.wait_for_timeout(3500)

            for cat, sels in CATEGORIES.items():
                if cat not in COMPOSE_CATEGORIES:
                    continue
                results = await _check(page, sels)
                ok, _ = _summarize("compose", cat, results)
                category_ok[cat] = ok

            # 結果サマリ
            print("\n" + "=" * 70)
            print("  結果サマリ")
            print("=" * 70)
            all_ok = True
            for cat, ok in category_ok.items():
                mark = "✅ OK" if ok else "❌ 全滅"
                print(f"  {mark}  {cat}")
                if not ok:
                    all_ok = False

            if not all_ok and dump_dom:
                print("\n⚠️ 全滅したカテゴリあり → 現在の DOM 候補をダンプします")
                await _dump_dom_candidates(page)

            if all_ok:
                print("\n🎉 全セレクタ OK — 現行 X UI と整合しています")
                return 0
            else:
                print("\n🚨 1つ以上のカテゴリで全滅 → x_publisher.py のセレクタ更新が必要")
                print("   --dump-dom で DOM 候補を列挙できます")
                return 1
        finally:
            # 誤投稿防止: 入力も submit もしていないが、念のため閉じる
            try:
                await context.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="X UI セレクタ存在確認（実投稿禁止）")
    parser.add_argument("--headless", action="store_true", help="ヘッドレスで実行")
    parser.add_argument("--dump-dom", action="store_true", help="全滅時に現在 DOM の候補を列挙")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(headless=args.headless, dump_dom=args.dump_dom))
    except KeyboardInterrupt:
        print("\n中断されました")
        return 130


if __name__ == "__main__":
    sys.exit(main())
