"""X(Twitter) セッション初期化スタンドアロンスクリプト。

天皇が x.com にログインして .x-session.json を作成するためだけの
スクリプト。x_publisher.py の自動投稿フローとは独立しており、
cron では呼ばれない（手動実行専用）。

v2 (2026-04): Playwright 素立ち上げでは X の自動化検知に弾かれ
「次へ」ボタンが反応しなかったため、launch_persistent_context +
実機 Chrome + ヒューマンライク設定 (stealth / UA / locale / timezone /
navigator.webdriver 偽装) で起動する方式に変更。

プロファイル永続化により 2 回目以降はログイン済み状態で立ち上がり、
storage_state を .x-session.json に保存して x_publisher.py 側と
互換を保つ。

使い方:
    python3 -m src.x_auth_init
    # or
    scripts/x_auth_init.sh
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_PATH = BASE_DIR / ".x-session.json"

# 永続プロファイル（ログインCookie・ローカルストレージを長期保存）。
# Playwright 既定の user-data-dir はテスト都度破棄されるため、
# 明示的にホーム配下の固定ディレクトリを使う。
PROFILE_DIR = Path.home() / ".x-playwright-profile"

# x_publisher.py と揃えたフィンガープリント（2026-04 時点 macOS Chrome 実在 UA）
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1440, "height": 900}
LOCALE = "ja-JP"
TIMEZONE = "Asia/Tokyo"

LOGIN_TIMEOUT_SEC = 600  # 10分


async def _apply_stealth(context) -> None:
    """playwright-stealth で navigator.webdriver 等 JS 側検知を封じる。失敗は握りつぶす。"""
    try:
        from playwright_stealth import Stealth

        stealth = Stealth()
        await stealth.apply_stealth_async(context)
    except Exception as e:
        print(f"  ⚠ stealth 適用失敗（続行）: {e}")


async def _run() -> int:
    print("=" * 60)
    print("  X(Twitter) セッション初期化 (persistent context / 実機Chrome)")
    print("=" * 60)
    if SESSION_PATH.exists():
        print(f"既存セッション: {SESSION_PATH}")
        print("  → 上書きします（Ctrl+C で中止）")
    print(f"プロファイル: {PROFILE_DIR}")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # launch_persistent_context: 実機 Chrome を headless=False で起動し、
        # プロファイルを永続化して X 側の自動化検知を回避する
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",  # 実機 Chrome（Chromium バンドルは使わない）
                headless=False,
                user_agent=USER_AGENT,
                viewport=VIEWPORT,
                locale=LOCALE,
                timezone_id=TIMEZONE,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        except Exception as e:
            print("❌ 実機 Chrome の起動に失敗しました。")
            print('   channel="chrome" は macOS の /Applications/Google Chrome.app を必要とします。')
            print("   Google Chrome をインストール後、再実行してください。")
            print(f"   詳細: {e}")
            sys.exit(1)

        try:
            # navigator.webdriver / languages / platform 偽装（x_publisher と同等）
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});"
                "Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});"
            )
            await _apply_stealth(context)

            # persistent_context は既定で about:blank ページを 1 枚持つ
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(
                    "https://x.com/login",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                print(f"❌ ログインページに到達できません: {e}")
                return 1

            print("🔑 ブラウザで X にログインしてください（2要素認証可）")
            print(f"   ログイン完了を自動検知します（最大 {LOGIN_TIMEOUT_SEC // 60} 分待機）...")
            print("   2回目以降はプロファイル再利用で即ログイン済み状態になります")

            waited = 0
            while True:
                url = page.url
                # ログイン完了後は /home へ遷移 or /login, /i/flow/login から抜ける
                logged_in = (
                    "/home" in url
                    or (
                        "x.com" in url
                        and "/login" not in url
                        and "/i/flow/login" not in url
                        and "/i/flow" not in url
                    )
                )
                if logged_in and waited >= 2:
                    break
                await page.wait_for_timeout(2000)
                waited += 2
                if waited >= LOGIN_TIMEOUT_SEC:
                    print(f"❌ {LOGIN_TIMEOUT_SEC}秒経過、ログイン未完了のため中断")
                    return 1
                if waited % 30 == 0:
                    print(f"   ...待機中 ({waited}秒経過、現在URL: {page.url})")

            print(f"✅ ログイン完了: {page.url}")
            # persistent_context でも storage_state() は取得可能。
            # x_publisher.py は launch + new_context(storage_state=...) 方式で
            # .x-session.json を読むため、ここで明示書き出して互換を維持する
            await context.storage_state(path=str(SESSION_PATH))
            print(f"✅ セッション保存: {SESSION_PATH}")
            print(f"✅ プロファイル保存: {PROFILE_DIR}")
            print("   → 次回の cron 投稿から自動で使用されます")
            return 0
        finally:
            await context.close()


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n中断されました")
        return 130


if __name__ == "__main__":
    sys.exit(main())
