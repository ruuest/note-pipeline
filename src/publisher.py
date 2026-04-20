import asyncio
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
)

from src.models import Article, PostResult

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_PATH = BASE_DIR / ".note-session.json"
SCREENSHOTS_DIR = BASE_DIR / "logs" / "screenshots"
TELEGRAM_NOTIFY = Path("/Users/apple/NorthValueAsset/cabinet/scripts/telegram_notify.sh")


class NoteSessionError(RuntimeError):
    """note セッション関連の致命的エラー（再ログイン必須）。"""


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

    def _notify_session_issue(self, detail: str) -> None:
        """セッション関連エラーを Telegram に即通知（失敗しても握りつぶす）。"""
        try:
            if TELEGRAM_NOTIFY.exists() and os.access(TELEGRAM_NOTIFY, os.X_OK):
                msg = (
                    "🔑 note セッション切れ、再ログインが必要\n"
                    f"原因: {detail}\n"
                    "対処: /Users/apple/NorthValueAsset/note-pipeline/scripts/note_auth_init.sh を実行"
                )
                subprocess.run(
                    [str(TELEGRAM_NOTIFY), msg],
                    timeout=10,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    async def _save_debug_screenshot(self, page: Page, tag: str) -> None:
        """セッション検証中の状態スクショ。失敗は無視。"""
        try:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"{tag}_{ts}.png"))
        except Exception:
            pass

    async def _get_context(self) -> BrowserContext:
        """保存済セッションから BrowserContext を復元する。

        方針:
          - Session ファイルが無い / 明示的に期限切れ → NoteSessionError を raise
            し Telegram 通知。cron で headless=False の手動ログインは実用的でないため、
            再認証は scripts/note_auth_init.sh で別途明示的に行う運用とする。
          - Timeout / ネットワークエラー → session を削除せず例外を伝搬
            （一時的な瞬断でセッションを殺さないため）。
        """
        if not SESSION_PATH.exists():
            self._notify_session_issue(f"{SESSION_PATH.name} が存在しない（未ログイン）")
            raise NoteSessionError(
                f"{SESSION_PATH} が存在しません。scripts/note_auth_init.sh で再認証してください。"
            )

        context = await self.browser.new_context(storage_state=str(SESSION_PATH))
        page = await context.new_page()
        try:
            # タイムアウトを 60秒に緩和（note.com 側の遅延 / ネットワーク変動を吸収）
            await page.goto(
                "https://note.com/dashboard",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(2000)
            if "/login" in page.url:
                # 明示的なセッション切れ（/login へリダイレクト）
                await self._save_debug_screenshot(page, "session_expired")
                await page.close()
                await context.close()
                SESSION_PATH.unlink(missing_ok=True)
                self._notify_session_issue("dashboard 遷移で /login にリダイレクトされた")
                raise NoteSessionError(
                    "note セッションが期限切れです。"
                    "scripts/note_auth_init.sh で再認証してください。"
                )
            await page.close()
            return context
        except PlaywrightTimeoutError as e:
            # ネットワーク瞬断等 → セッションを削除せず例外伝搬
            await self._save_debug_screenshot(page, "session_timeout")
            try:
                await page.close()
            except Exception:
                pass
            await context.close()
            print(f"  ⚠ セッション検証が Timeout（セッション保全して中断）: {e}")
            raise
        except NoteSessionError:
            raise
        except Exception as e:
            # 想定外エラーもセッションは保全、スクショだけ残して再raise
            await self._save_debug_screenshot(page, "session_unknown")
            try:
                await page.close()
            except Exception:
                pass
            await context.close()
            print(f"  ⚠ セッション検証中の不明エラー（セッション保全して中断）: {e}")
            raise

    async def publish(self, article: Article, dry_run: bool = False) -> PostResult:
        """note へ投稿する。

        Args:
            article: 投稿する記事（image_path があれば見出し画像としてアップロード）
            dry_run: True の場合、「公開に進む」以降のボタンは押さず
                     スクショのみで返す。live セッションを汚さずにUIを検証したい時に使う。
        """
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

            # 見出し画像（アイキャッチ）をアップロード。失敗しても投稿は続行。
            if article.image_path:
                await self._upload_cover_image(page, Path(article.image_path))
                await page.wait_for_timeout(1000)

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
            target_url = "https://nvcloud-lp.pages.dev/"
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

            # ドライランモード: 公開ボタンは押さず pre_publish スクショのみで返す
            if dry_run:
                print(f"  🧪 DRY RUN: 公開をスキップ（スクショ: pre_publish_{timestamp}.png）")
                return PostResult(
                    article=article,
                    success=True,
                    note_url=f"dry-run://{page.url}",
                    posted_at=datetime.now(),
                )

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

    async def _upload_cover_image(self, page: Page, image_path: Path) -> bool:
        """note エディタに見出し画像（アイキャッチ）をアップロード。

        失敗しても本文投稿は続行できるよう、例外は内部で握りつぶして False を返す。
        note の UI は変動するため、ボタン・確定 UI は複数セレクタで順次試す。
        """
        if not image_path or not image_path.exists():
            print(f"  ⚠ 画像ファイルが存在しない: {image_path}")
            return False

        # 1. 「見出し画像を追加」ボタン候補（note UI 変更に備え多段 fallback）
        trigger_selectors = [
            'button[aria-label*="見出し画像"]',
            'button[aria-label*="画像を追加"]',
            'button:has-text("見出し画像を追加")',
            'button:has-text("画像を追加")',
            'button[data-testid*="cover"]',
            'button[data-testid*="eyecatch"]',
        ]
        clicked = False
        for sel in trigger_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=3000)
                    clicked = True
                    print(f"  🖼 見出し画像ボタンクリック: {sel}")
                    break
            except Exception:
                continue
        if not clicked:
            print("  ⚠ 見出し画像ボタンが見つからない（note UI変更の可能性、投稿は続行）")
            return False

        await page.wait_for_timeout(1500)

        # 2. ファイル選択: まず input[type=file] 直接投入を試み、だめなら file_chooser ルート
        try:
            file_input = page.locator('input[type="file"]').first
            if await file_input.count() > 0:
                await file_input.set_input_files(str(image_path))
            else:
                async with page.expect_file_chooser(timeout=6000) as fc_info:
                    upload_selectors = [
                        'button:has-text("画像をアップロード")',
                        'button:has-text("画像を選択")',
                        'button:has-text("アップロード")',
                        'label:has-text("画像")',
                    ]
                    for sel in upload_selectors:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() > 0:
                                await loc.click(timeout=3000)
                                break
                        except Exception:
                            continue
                file_chooser = await fc_info.value
                await file_chooser.set_files(str(image_path))
        except Exception as e:
            print(f"  ⚠ 画像ファイル投入失敗（投稿続行）: {e}")
            return False

        await page.wait_for_timeout(3000)

        # 3. 確定ボタン（ある場合）
        confirm_selectors = [
            'button:has-text("保存")',
            'button:has-text("適用")',
            'button:has-text("確定")',
            'button:has-text("この画像を使用")',
            'button:has-text("完了")',
        ]
        for sel in confirm_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        print(f"  🖼 見出し画像アップロード完了: {image_path.name}")
        return True


def publish_article(article: Article, dry_run: bool = False) -> PostResult:
    """同期ラッパー。dry_run=True で公開ボタンをスキップ。"""
    return asyncio.run(_publish(article, dry_run=dry_run))


async def _publish(article: Article, dry_run: bool = False) -> PostResult:
    publisher = NotePublisher()
    await publisher.start()
    try:
        result = await publisher.publish(article, dry_run=dry_run)
        return result
    finally:
        await publisher.stop()
