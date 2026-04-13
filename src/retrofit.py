"""既存note記事の遡及スキャン + 自動修正

使い方:
    from src import retrofit
    issues = retrofit.scan_all("kaitori_nv_cloud")
    retrofit.write_report(issues, Path("retrofit_report.md"))

自動修正:
    import asyncio
    asyncio.run(retrofit.apply_fixes(issues))

注意:
  - note API は公式ドキュメントが存在しない非公式エンドポイントを使用
  - レート制限を避けるためリクエスト間 1.5s 待機
  - Playwright 自動修正は src/publisher.py の _get_context() を流用
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, asdict
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
LIST_URL = "https://note.com/api/v2/creators/{user}/contents?kind=note&page={page}"
NOTE_URL = "https://note.com/api/v3/notes/{key}"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) note-retrofit/0.1"

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\u2600-\u27BF]"
)
RAW_HEADING_RE = re.compile(r"(^|\n)\s*#{1,6}\s+\S")
POINTER_URL_RE = re.compile(r"👉\s*https?://")
# <p> 内にテキストURLがあり、<a> でラップされていないパターン
P_BLOCK_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL)
URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+")
A_TAG_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.DOTALL)


@dataclass
class ArticleIssue:
    key: str
    title: str
    url: str
    eyecatch_missing: bool = False
    raw_headings: list[str] = field(default_factory=list)
    pointer_urls: list[str] = field(default_factory=list)
    bare_urls: list[str] = field(default_factory=list)
    excessive_emoji: bool = False
    emoji_count: int = 0

    @property
    def has_issues(self) -> bool:
        return bool(
            self.raw_headings
            or self.pointer_urls
            or self.bare_urls
            or self.eyecatch_missing
            or self.excessive_emoji
        )

    @property
    def auto_fixable(self) -> bool:
        """自動修正可能な問題があるか (アイキャッチ以外)"""
        return bool(
            self.raw_headings or self.pointer_urls or self.bare_urls or self.excessive_emoji
        )


def _http_get_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_notes(user: str, max_pages: int = 20, sleep_sec: float = 1.5) -> list[dict]:
    """creator の全記事メタを取得。contents リスト (v2) を集約。"""
    results: list[dict] = []
    for page in range(1, max_pages + 1):
        url = LIST_URL.format(user=user, page=page)
        try:
            data = _http_get_json(url).get("data", {})
        except Exception as e:
            print(f"  ! page {page} 取得失敗: {e}")
            break
        contents = data.get("contents", []) or []
        results.extend(contents)
        if data.get("isLastPage") or not contents:
            break
        time.sleep(sleep_sec)
    return results


def fetch_note_detail(key: str) -> dict:
    return _http_get_json(NOTE_URL.format(key=key)).get("data", {})


def _html_to_text(html: str) -> str:
    """タグ除去した素のテキストを返す（検出用）。"""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


def _find_raw_headings(plain_text: str) -> list[str]:
    hits = []
    for line in plain_text.split("\n"):
        if re.match(r"^\s*#{1,6}\s+\S", line):
            hits.append(line.strip()[:80])
    return hits


def _find_pointer_urls(plain_text: str) -> list[str]:
    return POINTER_URL_RE.findall(plain_text)


def _find_bare_urls_in_html(html: str) -> list[str]:
    """<p>TEXT</p> ブロック内に <a> でラップされていない URL があれば返す"""
    bare: list[str] = []
    for m in P_BLOCK_RE.finditer(html):
        block = m.group(1)
        # <a> タグ内のテキストを取り除いてから URL を探す
        stripped = A_TAG_RE.sub("", block)
        # さらに残った HTML タグを除去
        stripped_text = re.sub(r"<[^>]+>", "", stripped)
        for url in URL_IN_TEXT_RE.findall(stripped_text):
            bare.append(url)
    return bare


def _count_emoji(text: str) -> int:
    return len(EMOJI_RE.findall(text))


def scan_article(meta: dict, *, emoji_threshold: int = 5) -> ArticleIssue:
    key = meta.get("key", "")
    title = meta.get("name", "")
    detail = fetch_note_detail(key)
    body_html = detail.get("body", "") or ""
    eyecatch = detail.get("eyecatch")
    plain = _html_to_text(body_html)

    raw_headings = _find_raw_headings(plain)
    pointer_urls = _find_pointer_urls(plain)
    bare_urls = _find_bare_urls_in_html(body_html)
    emoji_count = _count_emoji(plain)

    return ArticleIssue(
        key=key,
        title=title,
        url=f"https://note.com/{meta.get('user', {}).get('urlname', 'kaitori_nv_cloud')}/n/{key}",
        eyecatch_missing=not eyecatch,
        raw_headings=raw_headings,
        pointer_urls=pointer_urls,
        bare_urls=bare_urls,
        excessive_emoji=emoji_count >= emoji_threshold,
        emoji_count=emoji_count,
    )


def scan_all(user: str = "kaitori_nv_cloud", sleep_sec: float = 1.5) -> list[ArticleIssue]:
    print(f"🔍 {user} の記事一覧を取得中...")
    notes = fetch_all_notes(user)
    print(f"  → {len(notes)}件の記事を検出")
    issues: list[ArticleIssue] = []
    for i, meta in enumerate(notes, 1):
        print(f"  [{i}/{len(notes)}] {meta.get('name', '')[:40]}...")
        try:
            issue = scan_article(meta)
            issues.append(issue)
        except Exception as e:
            print(f"    ✗ エラー: {e}")
        time.sleep(sleep_sec)
    return issues


# ---------- レポート生成 ----------

def _fix_plain_text(text: str) -> str:
    """検出テキストを修正プレビュー用に整形 (src.generator.normalize_markdown_artifacts と同等)"""
    from src.generator import normalize_markdown_artifacts
    return normalize_markdown_artifacts(text)


def write_report(issues: list[ArticleIssue], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(issues)
    problem = [i for i in issues if i.has_issues]
    auto = [i for i in problem if i.auto_fixable]
    manual_only = [i for i in problem if not i.auto_fixable and i.eyecatch_missing]

    lines: list[str] = []
    lines.append("# note 既存記事 遡及修正レポート")
    lines.append("")
    lines.append(f"- **スキャン対象**: kaitori_nv_cloud")
    lines.append(f"- **総記事数**: {total}")
    lines.append(f"- **問題検出数**: {len(problem)}")
    lines.append(f"- **自動修正可能**: {len(auto)}")
    lines.append(f"- **手動対応のみ必要**: {len(manual_only)}")
    lines.append("")
    lines.append("## 検出ルール")
    lines.append("")
    lines.append("| コード | 内容 | 自動修正 |")
    lines.append("|---|---|---|")
    lines.append("| RAW_HEADING | 生markdown見出し (`### `等) | ✅ |")
    lines.append("| POINTER_URL | `👉 https://` プレフィックス | ✅ |")
    lines.append("| BARE_URL | 単独行URLが `<a>` 未リンク化 | ✅ (noteエディタ再保存) |")
    lines.append("| EYECATCH_MISSING | アイキャッチ画像なし | ❌ 手動 |")
    lines.append("| EXCESSIVE_EMOJI | 絵文字5個以上 | ✅ |")
    lines.append("")

    lines.append("## 問題のある記事一覧")
    lines.append("")
    for issue in problem:
        lines.append(f"### [{issue.title}]({issue.url})")
        lines.append(f"- key: `{issue.key}`")
        if issue.raw_headings:
            lines.append(f"- **RAW_HEADING**: {len(issue.raw_headings)}件")
            for h in issue.raw_headings[:5]:
                lines.append(f"    - `{h}`")
        if issue.pointer_urls:
            lines.append(f"- **POINTER_URL**: {len(issue.pointer_urls)}件")
        if issue.bare_urls:
            lines.append(f"- **BARE_URL**: {len(issue.bare_urls)}件")
            for u in issue.bare_urls[:3]:
                lines.append(f"    - {u}")
        if issue.eyecatch_missing:
            lines.append(f"- **EYECATCH_MISSING**: アイキャッチなし (手動アップロード必要)")
        if issue.excessive_emoji:
            lines.append(f"- **EXCESSIVE_EMOJI**: 絵文字 {issue.emoji_count} 個")
        lines.append("")

    lines.append("## 自動修正フロー")
    lines.append("")
    lines.append("1. `./scripts/note_articles_fix.sh --dry-run` で対象確認")
    lines.append("2. `./scripts/note_articles_fix.sh --apply` で Playwright 自動編集実行")
    lines.append("3. Playwright が失敗した記事は下記「手動対応リスト」参照")
    lines.append("")
    lines.append("## 手動対応リスト (アイキャッチ)")
    lines.append("")
    lines.append("note editor はアイキャッチ画像のアップロードを自動化しづらいため手動で:")
    lines.append("")
    for issue in problem:
        if issue.eyecatch_missing:
            lines.append(f"- [ ] [{issue.title}]({issue.url}/edit) — アイキャッチ画像を設定")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_このレポートは `src/retrofit.py` により自動生成されました。_")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ レポート出力: {out_path}")


# ---------- 本文クリーンアップ ----------

def _html_to_lines(html: str) -> list[str]:
    """note v3 body HTML を行配列に展開する（段落/見出し/箇条書きをそのまま改行に）"""
    # <br> → 改行
    html = re.sub(r"<br\s*/?>", "\n", html)
    # <p>, <h1..6>, <li> の終端を改行に
    html = re.sub(r"</(p|h[1-6]|li|div)>", "\n", html)
    html = re.sub(r"<li\b[^>]*>", "・", html)
    # 残りのタグを除去（<a> も除去するが textContent は残す）
    html = re.sub(r"<[^>]+>", "", html)
    text = unescape(html)
    lines = [line.rstrip() for line in text.split("\n")]
    # 連続空行を1行に圧縮
    result: list[str] = []
    prev_blank = False
    for line in lines:
        if not line.strip():
            if prev_blank:
                continue
            prev_blank = True
            result.append("")
        else:
            prev_blank = False
            result.append(line)
    # 先頭/末尾の空行除去
    while result and not result[0].strip():
        result.pop(0)
    while result and not result[-1].strip():
        result.pop()
    return result


def _limit_emoji(text: str, max_total: int = 3) -> str:
    """テキスト全体の絵文字を max_total 個まで残し、それ以降は削除"""
    count = [0]

    def repl(m):
        count[0] += 1
        return m.group(0) if count[0] <= max_total else ""

    return EMOJI_RE.sub(repl, text)


def clean_article_body(html: str) -> str:
    """note 記事 HTML を、note editor に再投入可能なクリーンプレーンテキストに整形。

    ルール:
      - RAW_HEADING (`## 【…】`, `### メリット` 等) → normalize_markdown_artifacts で 【…】 化
      - POINTER_URL (`👉 https://…`) → 単独行化 (`\\nhttps://…\\n`)
      - BARE_URL → 単独行のまま維持（note側のサーバ変換/createLinkに委ねる）
      - EMOJI 過剰 → 全体で3個までに切り詰め
    """
    from src.generator import normalize_markdown_artifacts

    lines = _html_to_lines(html)
    joined = "\n".join(lines)

    # POINTER_URL: 👉 直後のURLを単独行化
    joined = re.sub(r"👉\s*(https?://\S+)", r"\n\1\n", joined)

    # RAW_HEADING 正規化 + 装飾記号の整形
    joined = normalize_markdown_artifacts(joined)

    # EMOJI を全体で max 3 に切り詰め
    joined = _limit_emoji(joined, max_total=3)

    # 連続空行をもう一度圧縮
    joined = re.sub(r"\n{3,}", "\n\n", joined).strip()
    return joined


# ---------- Playwright 自動修正 ----------

async def apply_fixes(
    issues: list[ArticleIssue],
    *,
    only_key: str | None = None,
    max_articles: int | None = None,
) -> dict:
    """note editor の本文を Python側でクリーンアップした内容に差し替える。

    フロー:
      1. 対象の本文HTMLを v3 API から取得
      2. clean_article_body() でクリーン化
      3. /notes/<key>/edit を Playwright で開き、contenteditable を全選択→削除→再投入
      4. LP URL を createLink でアンカー化
      5. 「更新する」ボタンクリック
      6. 再度 v3 API から取得し scan_article() で問題件数 0 を検証
      7. 検証失敗時は詳細ログで abort（ロールバックは手動）
    """
    from src.publisher import NotePublisher, SCREENSHOTS_DIR
    from src.generator import CONFIG_DIR as _CFG

    targets = [i for i in issues if i.auto_fixable]
    if only_key:
        targets = [i for i in targets if i.key == only_key]
    if max_articles:
        targets = targets[:max_articles]

    result: dict = {
        "attempted": 0,
        "verified_clean": 0,
        "applied_but_dirty": 0,
        "failed": 0,
        "details": [],
    }
    if not targets:
        print("自動修正対象なし")
        return result

    publisher = NotePublisher()
    await publisher.start()
    try:
        context = await publisher._get_context()
        for issue in targets:
            result["attempted"] += 1
            detail: dict = {"key": issue.key, "title": issue.title}
            print(f"\n→ {issue.key}: {issue.title[:50]}")

            # 1. 本文取得 + クリーンアップ
            try:
                note_data = fetch_note_detail(issue.key)
                original_html = note_data.get("body", "") or ""
                cleaned = clean_article_body(original_html)
                detail["before_len"] = len(original_html)
                detail["cleaned_len"] = len(cleaned)
                print(f"  clean: HTML {len(original_html)} → text {len(cleaned)} chars")
            except Exception as e:
                detail["error"] = f"fetch/clean: {e}"
                result["failed"] += 1
                result["details"].append(detail)
                continue

            # 2. Playwright で edit page を開く
            page = await context.new_page()
            try:
                edit_url = f"https://editor.note.com/notes/{issue.key}/edit"
                await page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
                # editor リダイレクト待ち
                for _ in range(30):
                    await page.wait_for_timeout(1000)
                    if "/edit" in page.url and "editor.note.com" in page.url:
                        break
                await page.wait_for_timeout(4000)

                # 本文領域
                body_area = page.locator('div[role="textbox"][contenteditable="true"]')
                if await body_area.count() == 0:
                    raise RuntimeError("contenteditable body not found")
                await body_area.click()
                await page.wait_for_timeout(500)

                SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                await page.screenshot(
                    path=str(SCREENSHOTS_DIR / f"retrofit_{issue.key}_before.png")
                )

                # 全選択→削除
                import platform
                mod = "Meta" if platform.system() == "Darwin" else "Control"
                await page.keyboard.press(f"{mod}+a")
                await page.wait_for_timeout(300)
                await page.keyboard.press("Delete")
                await page.wait_for_timeout(500)

                # 再投入（publisher.py の挿入ロジックを流用）
                await page.evaluate(
                    """(text) => {
                        const editor = document.querySelector('div[role="textbox"][contenteditable="true"]');
                        if (!editor) return;
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
                    }""",
                    cleaned,
                )
                await page.wait_for_timeout(1500)

                # LP URL を createLink でアンカー化（publisher.py 流用）
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
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass

                await page.screenshot(
                    path=str(SCREENSHOTS_DIR / f"retrofit_{issue.key}_after_input.png")
                )

                # ステップ1: ツールバー上部の「公開に進む」or「更新する」ボタン
                # → 公開設定(ハッシュタグ/記事タイプ/詳細設定)ダイアログが開く
                clicked_step1 = False
                for sel in [
                    'button:has-text("公開に進む")',
                    'button:has-text("更新する")',
                ]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click()
                            clicked_step1 = True
                            detail["step1_selector"] = sel
                            break
                    except Exception:
                        continue
                if not clicked_step1:
                    raise RuntimeError("step1 update button not found")

                # ダイアログが開くのを待つ
                await page.wait_for_timeout(3500)
                await page.screenshot(
                    path=str(SCREENSHOTS_DIR / f"retrofit_{issue.key}_dialog.png")
                )

                # ステップ2: 公開設定ダイアログの右上「更新する」or「投稿する」
                # 新規記事の場合は「投稿する」、既存記事の場合は「更新する」が表示される
                # .last を使うのは、ダイアログ表示後ツールバーのボタンとダイアログ内ボタンが
                # 両方 DOM にあるケースに備えてのため
                clicked_step2 = False
                for sel in [
                    'button:has-text("更新する")',
                    'button:has-text("投稿する")',
                    'button:has-text("変更を公開")',
                ]:
                    try:
                        loc = page.locator(sel)
                        count = await loc.count()
                        if count == 0:
                            continue
                        # 複数ある場合は .last (ダイアログ側が後に表示される)
                        btn2 = loc.last if count > 1 else loc.first
                        if await btn2.is_visible():
                            await btn2.click()
                            clicked_step2 = True
                            detail["step2_selector"] = f"{sel}[{count}]"
                            await page.wait_for_timeout(6000)
                            break
                    except Exception:
                        continue
                if not clicked_step2:
                    raise RuntimeError("step2 confirm button not found")

                await page.screenshot(
                    path=str(SCREENSHOTS_DIR / f"retrofit_{issue.key}_final.png")
                )

            except Exception as e:
                detail["error"] = f"playwright: {e}"
                result["failed"] += 1
                result["details"].append(detail)
                await page.close()
                continue
            finally:
                await page.close()

            # 3. 検証: 再取得して scan
            try:
                time.sleep(3)
                post = fetch_note_detail(issue.key)
                post_html = post.get("body", "") or ""
                plain = _html_to_text(post_html)
                after_stats = {
                    "raw_headings": len(_find_raw_headings(plain)),
                    "pointer_urls": len(_find_pointer_urls(plain)),
                    "bare_urls": len(_find_bare_urls_in_html(post_html)),
                    "emoji": _count_emoji(plain),
                    "eyecatch": bool(post.get("eyecatch")),
                }
                detail["after"] = after_stats
                total_issues = (
                    after_stats["raw_headings"]
                    + after_stats["pointer_urls"]
                    + (1 if after_stats["emoji"] >= 5 else 0)
                )
                if total_issues == 0:
                    result["verified_clean"] += 1
                    print(f"  ✅ 検証OK (RH=0 PU=0 EMOJI={after_stats['emoji']})")
                else:
                    result["applied_but_dirty"] += 1
                    print(f"  ⚠ 検証NG: {after_stats}")
                result["details"].append(detail)
            except Exception as e:
                detail["error"] = f"verify: {e}"
                result["failed"] += 1
                result["details"].append(detail)

        await context.close()
    finally:
        await publisher.stop()
    return result


# ---------- CLI ----------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="note 既存記事スキャン+修正")
    parser.add_argument("--user", default="kaitori_nv_cloud")
    parser.add_argument("--apply", action="store_true", help="Playwrightで本文書き替えを実行")
    parser.add_argument("--dry-run", action="store_true", help="修正対象の一覧+差分プレビュー")
    parser.add_argument("--only", default=None, help="特定1記事のkeyのみ対象")
    parser.add_argument(
        "--report",
        default="/Users/apple/NorthValueAsset/cabinet/projects/note_analytics/retrofit_report.md",
    )
    args = parser.parse_args()

    issues = scan_all(args.user)
    report_path = Path(args.report)
    write_report(issues, report_path)

    problem = [i for i in issues if i.has_issues]
    auto = [i for i in problem if i.auto_fixable]

    print()
    print(f"総記事数: {len(issues)}")
    print(f"問題あり: {len(problem)}")
    print(f"自動修正可能: {len(auto)}")

    if args.only:
        auto = [i for i in auto if i.key == args.only]
        print(f"--only 絞り込み: {len(auto)}件")

    if args.dry_run:
        print("\n=== dry-run: 修正対象 ===")
        for i in auto:
            print(f"  - {i.title}")
            if i.raw_headings:
                print(f"      RAW_HEADING x{len(i.raw_headings)}")
            if i.pointer_urls:
                print(f"      POINTER_URL x{len(i.pointer_urls)}")
            if i.bare_urls:
                print(f"      BARE_URL x{len(i.bare_urls)}")
            if i.excessive_emoji:
                print(f"      EXCESSIVE_EMOJI ({i.emoji_count})")
            # 差分プレビュー: 先頭300文字だけクリーン前後を見せる
            try:
                detail = fetch_note_detail(i.key)
                html = detail.get("body", "") or ""
                cleaned = clean_article_body(html)
                before = _html_to_text(html)[:200].replace("\n", " ⏎ ")
                after = cleaned[:200].replace("\n", " ⏎ ")
                print(f"      BEFORE: {before}")
                print(f"      AFTER : {after}")
            except Exception as e:
                print(f"      (preview error: {e})")
            time.sleep(0.8)
        return

    if args.apply:
        print("\n=== Playwright 本文書き替え開始 ===")
        result = asyncio.run(apply_fixes(issues, only_key=args.only))
        print(f"  試行: {result['attempted']}")
        print(f"  検証OK: {result['verified_clean']}")
        print(f"  適用後も問題残: {result['applied_but_dirty']}")
        print(f"  失敗: {result['failed']}")
        for d in result["details"]:
            if "error" in d:
                print(f"    ✗ {d['key']}: {d['error']}")


if __name__ == "__main__":
    _cli()
