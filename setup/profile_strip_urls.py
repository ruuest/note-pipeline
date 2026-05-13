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


def _backup_profile(bio: str, name: str | None, ts: str | None = None) -> Path:
    """プロフィール bio を logs/profile_strip_backup_<ts>.html に保存。rollback 用。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS_DIR / f"profile_strip_backup_{ts}.html"
    path.write_text(
        f"<!-- profile backup {ts} -->\n"
        f"<!-- display_name: {name or '(unknown)'} -->\n"
        f"<bio>{bio}</bio>\n",
        encoding="utf-8",
    )
    return path


async def execute_strip_profile(updater: NoteProfileUpdater) -> int:
    """note プロフィール bio から URL を実削除して保存する。

    手順:
      1. 設定画面を開く
      2. 現在の bio を取得
      3. URL 削除版を strip_urls_from_plain_text で生成
      4. backup を logs/ に保存
      5. bio フィールドに削除版を fill
      6. 保存ボタンをクリック
      7. 失敗時は SystemExit (rollback はバックアップから手動で fill)
    """
    context = await updater._get_context()
    page = await context.new_page()
    try:
        await page.goto(SETTINGS_URL, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        if "/login" in page.url:
            print("  ✗ セッション無効。setup/profile_update.py で再ログイン後に再試行")
            return 1

        bio_input, bio_sel = await updater._find_bio_textarea(page)
        if bio_input is None:
            print("  ✗ 自己紹介フィールド未検出。setup/REBRAND_MANUAL.md 参照")
            await updater._screenshot(page, "exec_bio_not_found")
            return 1

        # 現在 bio を取得 + バックアップ
        current_bio = await bio_input.input_value()
        if not current_bio:
            print("  ✓ bio が空 → 削除対象なし")
            return 0
        cleaned, removed = strip_urls_from_plain_text(current_bio)
        if not removed:
            print("  ✓ bio に URL なし → 何もしない")
            return 0

        backup_path = _backup_profile(current_bio, None)
        print(f"  ✓ backup saved: {backup_path}")
        print(f"  削除対象 URL: {removed}")

        # bio を上書き
        await bio_input.click()
        await bio_input.fill("")
        await page.wait_for_timeout(300)
        await bio_input.fill(cleaned)
        await page.wait_for_timeout(500)
        print(f"  ✓ bio 上書き ({bio_sel}): {len(removed)} URL 削除")

        await updater._screenshot(page, "exec_before_save")

        save_sel = await updater._click_save(page)
        if save_sel is None:
            print("  ✗ 保存ボタン未検出 — 即停止 (バックアップから手動 rollback 必要)")
            await updater._screenshot(page, "exec_save_not_found")
            raise SystemExit(1)
        await page.wait_for_timeout(4000)
        await updater._screenshot(page, "exec_after_save")
        print(f"  ✓ 保存完了 (button: {save_sel})")
        return 0
    except SystemExit:
        raise
    except Exception as e:
        print(f"  ✗ 例外発生 — 即停止: {e}")
        await updater._screenshot(page, "exec_error")
        raise SystemExit(1)
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

    # --execute (Phase 2 — 凌佳承認後にのみ起動すること)
    print("=== EXECUTE MODE — note プロフィール bio から URL 削除 ===")
    print("⚠ 不可逆操作: バックアップは logs/profile_strip_backup_*.html\n")
    updater = NoteProfileUpdater()
    asyncio.run(_run_execute(updater))


async def _run_execute(updater: NoteProfileUpdater) -> None:
    await updater.start()
    try:
        rc = await execute_strip_profile(updater)
        if rc != 0:
            raise SystemExit(rc)
    finally:
        await updater.stop()


if __name__ == "__main__":
    main()
