"""note セッション初期化スタンドアロンスクリプト。

天皇が note にログインして .note-session.json を作成するためだけの
スクリプト。publisher.py の自動投稿フローとは独立しており、
cron では呼ばれない（手動実行専用）。

使い方:
    python3 -m src.auth_init
    # or
    scripts/note_auth_init.sh
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_PATH = BASE_DIR / ".note-session.json"

LOGIN_TIMEOUT_SEC = 600  # 10分


async def _run() -> int:
    print("=" * 60)
    print("  note セッション初期化")
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
                    "https://note.com/login",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                print(f"❌ ログインページに到達できません: {e}")
                return 1

            print("🔑 ブラウザで note にログインしてください（Googleログイン可）")
            print(f"   ログイン完了を自動検知します（最大 {LOGIN_TIMEOUT_SEC // 60} 分待機）...")

            waited = 0
            while "/login" in page.url or "accounts.google.com" in page.url:
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
