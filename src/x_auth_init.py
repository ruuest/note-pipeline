"""X(Twitter) セッション初期化スタンドアロンスクリプト。

天皇が x.com にログインして .x-session.json を作成するためだけの
スクリプト。x_publisher.py の自動投稿フローとは独立しており、
cron では呼ばれない（手動実行専用）。

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

LOGIN_TIMEOUT_SEC = 600  # 10分


async def _run() -> int:
    print("=" * 60)
    print("  X(Twitter) セッション初期化")
    print("=" * 60)
    if SESSION_PATH.exists():
        print(f"既存セッション: {SESSION_PATH}")
        print("  → 上書きします（Ctrl+C で中止）")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            context = await browser.new_context()
            page = await context.new_page()
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

            waited = 0
            # ログイン完了後は /home へ遷移する（または /i/flow/login から抜ける）
            while True:
                url = page.url
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
            await context.storage_state(path=str(SESSION_PATH))
            print(f"✅ セッション保存: {SESSION_PATH}")
            print("   → 次回の cron 投稿から自動で使用されます")
            return 0
        finally:
            await browser.close()


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n中断されました")
        return 130


if __name__ == "__main__":
    sys.exit(main())
