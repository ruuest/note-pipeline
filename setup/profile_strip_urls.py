"""note プロフィール (自己紹介文 + ソーシャルリンク) から URL を全削除

天皇要件 (2026-05-12 15:06): NV CLOUD 他社販売見送り → プロフィール側の URL も全撤去。

3 段階フロー (本タスクの記事削除と同期):
  1. ``--dry-run`` で現在のプロフィール内容を取得して URL 検出結果を出力
  2. 凌佳承認 (PM 経由)
  3. ``--execute`` で適用 (Playwright login → 自己紹介上書き)

注意:
  - 表示名 (display_name) は本タスクでは触らない (URL 含まれない前提)
  - ソーシャルリンク欄が note UI にある場合の削除は Phase 2 で実装
    (現時点では bio テキスト内 URL の削除に限定)

CLI 例:
  uv run python -m setup.profile_strip_urls --dry-run
  uv run python -m setup.profile_strip_urls --execute  # 凌佳承認後のみ
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from src.strip_all_urls import strip_all_urls_from_html, BARE_URL_RE
from setup.profile_update import (
    NoteProfileUpdater,
    SETTINGS_URL,
    LOGS_DIR,
)

BASE_DIR = Path(__file__).resolve().parent.parent


def strip_urls_from_plain_text(text: str) -> tuple[str, list[str]]:
    """プロフィール文 (プレーンテキスト想定) から URL を削除。
    削除した URL のリストも返す。
    """
    removed: list[str] = []

    def repl(m):
        removed.append(m.group(0))
        return ""

    cleaned = BARE_URL_RE.sub(repl, text)
    # 削除痕の整理
    import re
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    return cleaned, removed


async def fetch_current_bio(updater: NoteProfileUpdater) -> tuple[str | None, str | None]:
    """note 設定画面から現在の bio (自己紹介) を取得 (read-only)。

    Returns:
      (bio_text, display_name)  — 取得失敗時は None
    """
    context = await updater._get_context()
    page = await context.new_page()
    try:
        await page.goto(SETTINGS_URL, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        if "/login" in page.url:
            return None, None

        bio_input, _ = await updater._find_bio_textarea(page)
        bio_text = await bio_input.input_value() if bio_input else None

        name_input, _ = await updater._find_display_name_input(page)
        name_text = await name_input.input_value() if name_input else None

        return bio_text, name_text
    finally:
        await page.close()
        await context.close()


async def dry_run() -> int:
    print("→ note 設定画面からプロフィールを取得中 ...")
    updater = NoteProfileUpdater()
    await updater.start()
    try:
        bio, name = await fetch_current_bio(updater)
    finally:
        await updater.stop()

    if bio is None:
        print("  ✗ プロフィール取得失敗 (セッション切れ等)。setup/profile_update.py で手動再ログイン後に再試行")
        return 1

    print(f"\n表示名: {name or '(取得不能)'}")
    print(f"自己紹介 (現在):\n  {bio!r}\n")

    cleaned, removed = strip_urls_from_plain_text(bio)
    print("=== 削除対象 URL ===")
    if not removed:
        print("  なし (URL 含まれず)")
        return 0
    for url in removed:
        print(f"  - {url}")
    print(f"\n合計 {len(removed)} URL 削除予定")
    print("\n=== 削除後 (preview) ===")
    print(cleaned)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    preview_path = LOGS_DIR / f"profile_strip_urls_dryrun_{ts}.txt"
    preview_path.write_text(
        f"BEFORE:\n{bio}\n\nREMOVED:\n" + "\n".join(removed) + f"\n\nAFTER:\n{cleaned}\n",
        encoding="utf-8",
    )
    print(f"\nプレビュー保存: {preview_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="note プロフィール内 URL 一括削除")
    parser.add_argument("--dry-run", action="store_true", help="削除内容を CLI 出力 (デフォルト)")
    parser.add_argument("--execute", action="store_true",
                        help="本実行。凌佳承認後にのみ使用")
    args = parser.parse_args()

    if args.execute and args.dry_run:
        print("error: --execute と --dry-run は併用不可")
        raise SystemExit(2)
    do_dry_run = args.dry_run or not args.execute

    if do_dry_run:
        rc = asyncio.run(dry_run())
        raise SystemExit(rc)

    print("=== EXECUTE モードは Phase 2 用、凌佳承認後に実装 ===")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
