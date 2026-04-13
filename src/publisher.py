import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

from src.models import Article, PostResult

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_PATH = BASE_DIR / ".note-session.json"
SCREENSHOTS_DIR = BASE_DIR / "logs" / "screenshots"


class NotePublisher:
    def __init__(self):
        self.email = os.environ.get("NOTE_EMAIL", "")
        self.password = os.environ.get("NOTE_PASSWORD", "")
        self.playwright = None
        self.browser = None

    async def start(self):
        self.playwright = await async_playwright().start()
        # headless=Falseが必要（noteエディタがheadlessモードで正常に動作しない）
        self.browser = await self.playwright.chromium.launch(headless=False)

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def _get_context(self) -> BrowserContext:
        if SESSION_PATH.exists():
            try:
                context = await self.browser.new_context(storage_state=str(SESSION_PATH))
                # セッション有効性チェック
                page = await context.new_page()
                await page.goto("https://note.com/dashboard", wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                if "/login" in page.url:
                    await page.close()
                    await context.close()
                    raise Exception("Session expired")
                await page.close()
                return context
            except Exception:
                SESSION_PATH.unlink(missing_ok=True)

        # セッション切れ → 手動ログインを促す
        context = await self.browser.new_context()
        page = await context.new_page()
        await page.goto("https://note.com/login", wait_until="domcontentloaded")
        print("セッション切れ。ブラウザで手動ログインしてください...")

        # ログイン完了を待つ
        while "/login" in page.url or "accounts.google.com" in page.url:
            await page.wait_for_timeout(2000)

        print(f"ログイン成功: {page.url}")
        await context.storage_state(path=str(SESSION_PATH))
        await page.close()
        return context

    async def publish(self, article: Article) -> PostResult:
        context = await self._get_context()
        page = await context.new_page()

        try:
            # 記事作成画面へ
            await page.goto("https://note.com/notes/new", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # エディタ画面のリダイレクトを待つ（editor.note.com に飛ぶ）
            for _ in range(60):
                await page.wait_for_timeout(1000)
                url = page.url
                if "editor.note.com" in url:
                    break
            else:
                raise Exception(f"Editor redirect timeout. URL: {page.url}")
            # /edit/ URLへの2段目リダイレクトを待つ
            for _ in range(30):
                await page.wait_for_timeout(1000)
                if "/edit/" in page.url:
                    break
            await page.wait_for_timeout(5000)

            # タイトル入力
            title_input = page.locator('textarea[placeholder="記事タイトル"]')
            await title_input.fill(article.title)
            await page.wait_for_timeout(500)

            # 本文入力 - contenteditable div[role=textbox]
            body_area = page.locator('div[role="textbox"][contenteditable="true"]')
            await body_area.click()
            await page.wait_for_timeout(500)

            # 本文をexecCommandで一括挿入（段落ごと）。
            # NOTE: URLの自動リンクカード化は execCommand では発火しない既知問題あり。
            # 当面はプレーンテキストでURLを挿入し、note側のサーバ側変換に委ねる。
            await page.evaluate("""(text) => {
                const editor = document.querySelector('div[role="textbox"][contenteditable="true"]');
                if (editor) {
                    editor.focus();
                    const lines = text.split('\\n');
                    lines.forEach((line, i) => {
                        if (line.trim() === '') {
                            document.execCommand('insertParagraph', false);
                        } else {
                            document.execCommand('insertText', false, line);
                        }
                        if (i < lines.length - 1) {
                            document.execCommand('insertParagraph', false);
                        }
                    });
                }
            }""", article.body)

            await page.wait_for_timeout(1500)

            # 本文中のCTA URLを <a> リンクに昇格させる（noteのリンクカード生成のヒント）。
            # JS走査で URL を含むテキストノードを見つけ、selection→execCommand('createLink')で
            # アンカー化する。失敗してもプレーンテキストで残るので安全。
            target_url = "https://nv-cloud-lp.onrender.com"
            try:
                await page.evaluate(
                    """(url) => {
                        const editor = document.querySelector('div[role="textbox"][contenteditable="true"]');
                        if (!editor) return;
                        const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const idx = node.textContent.indexOf(url);
                            if (idx === -1) continue;
                            const range = document.createRange();
                            range.setStart(node, idx);
                            range.setEnd(node, idx + url.length);
                            const sel = window.getSelection();
                            sel.removeAllRanges();
                            sel.addRange(range);
                            document.execCommand('createLink', false, url);
                            sel.removeAllRanges();
                            break;
                        }
                    }""",
                    target_url,
                )
                await page.wait_for_timeout(1500)
            except Exception:
                pass

            # スクリーンショット（デバッグ用）
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"pre_publish_{timestamp}.png"))

            # 「公開に進む」ボタンをクリック
            await page.locator('button:has-text("公開に進む")').click()
            await page.wait_for_timeout(3000)

            # 公開設定画面のスクリーンショット
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"publish_dialog_{timestamp}.png"))

            # 公開設定画面で「投稿する」ボタンをクリック
            await page.locator('button:has-text("投稿する")').click()
            await page.wait_for_timeout(8000)

            # 投稿URLを取得
            note_url = page.url
            if "/kaitori_nv_cloud/n/" not in note_url:
                # 投稿後のページURLが /n/ を含まないケース → 公開API経由で最新記事URLを取得
                # （旧実装は a[href*="/n/"].first を使ったが運営記事(/info/n/...)を拾う事故があった）
                import httpx

                try:
                    api_url = (
                        "https://note.com/api/v2/creators/kaitori_nv_cloud/contents"
                        "?kind=note&page=1"
                    )
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            api_url,
                            headers={"User-Agent": "Mozilla/5.0"},
                        )
                        data = resp.json()
                        contents = data.get("data", {}).get("contents", [])
                        if contents:
                            api_url_first = contents[0].get("noteUrl")
                            if api_url_first:
                                note_url = api_url_first
                except Exception:
                    pass

            await page.screenshot(path=str(SCREENSHOTS_DIR / f"post_publish_{timestamp}.png"))

            return PostResult(
                article=article,
                success=True,
                note_url=note_url,
                posted_at=datetime.now(),
            )

        except Exception as e:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"error_{timestamp}.png"))
            return PostResult(
                article=article,
                success=False,
                error=str(e),
            )

        finally:
            await page.close()
            await context.close()


def publish_article(article: Article) -> PostResult:
    """同期ラッパー"""
    return asyncio.run(_publish(article))


async def _publish(article: Article) -> PostResult:
    publisher = NotePublisher()
    await publisher.start()
    try:
        result = await publisher.publish(article)
        return result
    finally:
        await publisher.stop()
