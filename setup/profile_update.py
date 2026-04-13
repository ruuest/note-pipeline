"""
note プロフィール（表示名・プロフィール文）更新スクリプト

使い方:
  uv run python setup/profile_update.py \
      --display-name "出張買取DX研究所｜NV CLOUD公式" \
      --bio "出張買取業界のDXを研究・発信するNV CLOUD公式note..."

  # 表示名のみ更新
  uv run python setup/profile_update.py --display-name "出張買取DX研究所｜NV CLOUD公式"

note の設定画面はセレクタが頻繁に変わるため、いくつかの候補を順に試す。
自動化に失敗した場合は setup/REBRAND_MANUAL.md の手順で手動対応する。

src/publisher.py の _get_context() と同じセッション管理方式を流用。
"""
import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_PATH = BASE_DIR / ".note-session.json"
LOGS_DIR = Path(__file__).resolve().parent / "logs"

SETTINGS_URL = "https://note.com/settings/account"


class NoteProfileUpdater:
    def __init__(self):
        self.playwright = None
        self.browser = None

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=False)

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def _get_context(self) -> BrowserContext:
        """src/publisher.py と同じセッション管理"""
        if SESSION_PATH.exists():
            try:
                context = await self.browser.new_context(storage_state=str(SESSION_PATH))
                page = await context.new_page()
                await page.goto(
                    "https://note.com/dashboard",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                await page.wait_for_timeout(2000)
                if "/login" in page.url:
                    await page.close()
                    await context.close()
                    raise Exception("Session expired")
                await page.close()
                return context
            except Exception:
                SESSION_PATH.unlink(missing_ok=True)

        context = await self.browser.new_context()
        page = await context.new_page()
        await page.goto("https://note.com/login", wait_until="domcontentloaded")
        print("セッション切れ。ブラウザで手動ログインしてください...")
        while "/login" in page.url or "accounts.google.com" in page.url:
            await page.wait_for_timeout(2000)
        print(f"ログイン成功: {page.url}")
        await context.storage_state(path=str(SESSION_PATH))
        await page.close()
        return context

    async def _screenshot(self, page: Page, tag: str):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = LOGS_DIR / f"profile_{tag}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        print(f"  screenshot: {path}")

    async def _find_display_name_input(self, page: Page):
        """
        note の「名前」入力欄を複数セレクタで探す。
        HTML 構造が変わる可能性があるので fallback を用意。
        """
        candidates = [
            'input[name="nickname"]',
            'input[name="name"]',
            'input[placeholder*="名前"]',
            'input[aria-label*="名前"]',
            # label に「名前」を含むフォームの input
            'label:has-text("名前") >> xpath=following::input[1]',
            'div:has-text("名前") >> input[type="text"]',
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc, sel
            except Exception:
                continue
        return None, None

    async def _find_bio_textarea(self, page: Page):
        candidates = [
            'textarea[name="profile"]',
            'textarea[name="description"]',
            'textarea[placeholder*="自己紹介"]',
            'textarea[aria-label*="自己紹介"]',
            'label:has-text("自己紹介") >> xpath=following::textarea[1]',
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc, sel
            except Exception:
                continue
        return None, None

    async def _click_save(self, page: Page):
        candidates = [
            'button:has-text("保存")',
            'button:has-text("更新")',
            'button:has-text("変更を保存")',
            'button[type="submit"]',
        ]
        for sel in candidates:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                    await btn.click()
                    return sel
            except Exception:
                continue
        return None

    async def update(self, display_name: str, bio: str | None = None) -> bool:
        context = await self._get_context()
        page = await context.new_page()
        try:
            # 1) アカウント設定ページへ
            await page.goto(SETTINGS_URL, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
            await self._screenshot(page, "01_settings_opened")
            print(f"  url: {page.url}")

            # /login に飛ばされた場合はセッション無効
            if "/login" in page.url:
                print("  → セッション無効。再ログインが必要です。")
                return False

            # 2) 表示名フィールドを特定
            name_input, name_sel = await self._find_display_name_input(page)
            if name_input is None:
                print("  ✗ 表示名フィールドが見つかりません。")
                print("  → setup/REBRAND_MANUAL.md を参照して手動更新してください。")
                await self._screenshot(page, "02_name_not_found")
                return False

            print(f"  ✓ 表示名フィールド検出: {name_sel}")
            await name_input.click()
            await name_input.fill("")
            await page.wait_for_timeout(300)
            await name_input.fill(display_name)
            await page.wait_for_timeout(500)
            print(f"  ✓ 表示名を入力: {display_name}")

            # 3) プロフィール文（任意）
            if bio:
                bio_input, bio_sel = await self._find_bio_textarea(page)
                if bio_input is not None:
                    print(f"  ✓ 自己紹介フィールド検出: {bio_sel}")
                    await bio_input.click()
                    await bio_input.fill("")
                    await page.wait_for_timeout(300)
                    await bio_input.fill(bio)
                    await page.wait_for_timeout(500)
                    print("  ✓ 自己紹介を入力")
                else:
                    print("  △ 自己紹介フィールドは見つからず（表示名のみ更新）")

            await self._screenshot(page, "03_before_save")

            # 4) 保存ボタン
            save_sel = await self._click_save(page)
            if save_sel is None:
                print("  ✗ 保存ボタンが見つかりません。")
                await self._screenshot(page, "04_save_not_found")
                return False

            print(f"  ✓ 保存ボタンをクリック: {save_sel}")
            await page.wait_for_timeout(4000)
            await self._screenshot(page, "05_after_save")
            print("  ✓ 更新完了")
            return True

        except Exception as e:
            print(f"  ✗ エラー: {e}")
            await self._screenshot(page, "99_error")
            return False
        finally:
            await page.close()
            await context.close()


async def _run(display_name: str, bio: str | None):
    updater = NoteProfileUpdater()
    await updater.start()
    try:
        ok = await updater.update(display_name=display_name, bio=bio)
        return ok
    finally:
        await updater.stop()


def main():
    parser = argparse.ArgumentParser(description="note プロフィール更新")
    parser.add_argument(
        "--display-name",
        required=True,
        help="新しい表示名（例: 出張買取DX研究所｜NV CLOUD公式）",
    )
    parser.add_argument(
        "--bio",
        default=None,
        help="新しい自己紹介文（任意）",
    )
    args = parser.parse_args()

    ok = asyncio.run(_run(args.display_name, args.bio))
    if not ok:
        print("\n自動更新に失敗しました。setup/REBRAND_MANUAL.md を参照してください。")
        raise SystemExit(1)
    print("\n完了しました。")


if __name__ == "__main__":
    main()
