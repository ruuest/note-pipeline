"""note 既存記事から全 URL を一括削除するスクリプト

天皇要件 (2026-05-12 15:06): NV CLOUD 他社販売見送り → note 記事内 URL 全撤去
(本文の <a> リンク / リンクカード / bare URL すべて対象)。プロフィール側は
setup/profile_strip_urls.py を参照。

3 段階フロー (不可逆操作のため厳守):
  1. ``--dry-run`` で削除内容を CLI 出力 (note サーバーに影響なし)
  2. 凌佳承認 (PM 経由)
  3. ``--execute`` で適用 (Playwright login → 全選択 → 再投入)

レート制限 (memory: feedback_note_posting_limits):
  - 連続更新 30 分間隔
  - 1 日 3 本以下
  - 状態は ``.strip_state.json`` に永続化、再実行時に読み込み

バックアップ:
  - 各記事 body を ``logs/strip_url_backup_<key>_<timestamp>.html`` に保存
  - 実行ログを ``logs/strip_url_run_<timestamp>.log`` に保存
  - dry-run でも記事ごとのプレビューを log に追記

CLI 例:
  uv run python -m src.strip_all_urls --user kaitori_nv_cloud --dry-run --limit 2
  uv run python -m src.strip_all_urls --user kaitori_nv_cloud --execute --only <key>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, date
from html import unescape
from pathlib import Path

from src.retrofit import (
    fetch_all_notes,
    fetch_note_detail,
    _html_to_text,
    _html_to_lines,
)

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
STATE_PATH = BASE_DIR / ".strip_state.json"

# レート制限設定 (memory: feedback_note_posting_limits)
DAILY_LIMIT = 3
MIN_INTERVAL_SECONDS = 30 * 60  # 30 分

# ---------- URL 検出/削除パターン ----------
# 各パターンは _strip_one_pattern に従い (regex, replacement) のタプル。
# replacement が callable の場合は match を受け取って置換文字列を返す。

# <a href="...">テキスト</a> — テキストのみ残す。bare-URL テキスト (<a>https://…</a>) は
# 後段の bare URL 削除で消えるため、ここではテキスト保持で OK。
A_TAG_RE = re.compile(r'<a\b[^>]*\bhref=(?:"[^"]*"|\'[^\']*\'|[^\s>]+)[^>]*>(.*?)</a>',
                      re.DOTALL | re.IGNORECASE)

# note の embed (リンクカード) — 親要素含めてまるごと削除
EMBED_FIGURE_RE = re.compile(
    r'<figure\b[^>]*class=["\'][^"\']*embed[^"\']*["\'][^>]*>.*?</figure>',
    re.DOTALL | re.IGNORECASE,
)
EMBED_TAG_RE = re.compile(r'<embed\b[^>]*/?>', re.IGNORECASE)
# note v3 body には embedded-service blockquote が出ることもある
EMBED_BLOCKQUOTE_RE = re.compile(
    r'<blockquote\b[^>]*class=["\'][^"\']*embedly-card[^"\']*["\'][^>]*>.*?</blockquote>',
    re.DOTALL | re.IGNORECASE,
)

# bare URL (テキスト中に裸で残った http(s)://…)
# 属性値内の URL (data-src="https://..." 等) は誤検出するため、HTML タグを先に
# 全削除した上で適用する想定。残った text-only ストリームで検出する。
BARE_URL_RE = re.compile(r'https?://[^\s<>"\'）)、]+', re.IGNORECASE)

# 任意の embed 系要素 (note の external-article 等カスタムタグ含む) を削除
EMBED_CUSTOM_RE = re.compile(
    r'<(?:external-article|embed-card|note-embed)\b[^>]*>(?:.*?</[^>]+>)?',
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class UrlMatch:
    """1 つの URL 削除イベントを記録する。"""
    kind: str   # 'anchor' | 'self_link' | 'linkcard' | 'bare'
    url: str    # 削除対象 URL (linkcard 等で取得不能な場合は '<embed>')
    snippet: str  # 周辺テキスト (デバッグ用、最大 80 文字)


@dataclass
class ArticleStripResult:
    key: str
    title: str
    note_url: str
    original_html: str
    stripped_html: str
    matches: list[UrlMatch]

    @property
    def total_removed(self) -> int:
        return len(self.matches)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {"anchor": 0, "self_link": 0, "linkcard": 0, "bare": 0}
        for m in self.matches:
            out[m.kind] = out.get(m.kind, 0) + 1
        return out


# ---------- pure functions: ロジック (テスト容易) ----------

def _snippet(html: str, span: tuple[int, int], width: int = 60) -> str:
    start = max(0, span[0] - width // 2)
    end = min(len(html), span[1] + width // 2)
    raw = html[start:end].replace("\n", " ")
    return re.sub(r"\s+", " ", raw)[:80]


def _strip_anchors(html: str, matches: list[UrlMatch]) -> str:
    """<a href='X'>Y</a> → Y (テキストのみ残す)。

    Y がそれ自体 URL (self-link) の場合は kind=self_link で記録するが、
    inner text は保持して後段の bare URL 削除に任せる (重複削除回避)。
    """
    def repl(m: re.Match[str]) -> str:
        href_match = re.search(r'href=(["\']?)([^"\'\s>]+)\1', m.group(0), re.IGNORECASE)
        href = href_match.group(2) if href_match else ""
        inner = m.group(1) or ""
        # inner_text plain (タグ除去済) で URL かどうか判定
        inner_text = re.sub(r"<[^>]+>", "", inner).strip()
        kind = "self_link" if inner_text == href and href else "anchor"
        matches.append(UrlMatch(
            kind=kind,
            url=href or "<unknown>",
            snippet=_snippet(html, m.span()),
        ))
        # self_link はタグごと削除 (中身も href と同じ URL → 残すと bare URL になる)。
        # anchor は inner を残す (テキストとして意味があるため)。
        if kind == "self_link":
            return ""
        return inner
    return A_TAG_RE.sub(repl, html)


def _strip_linkcards(html: str, matches: list[UrlMatch]) -> str:
    """note のリンクカード (figure.embed / embed タグ / embedly-card blockquote /
    external-article カスタム要素) を削除。"""
    for pattern in (EMBED_FIGURE_RE, EMBED_BLOCKQUOTE_RE, EMBED_CUSTOM_RE, EMBED_TAG_RE):
        def repl(m: re.Match[str]) -> str:
            # URL を best-effort で抽出 (data-src / href / 内部 bare URL)
            inner = m.group(0)
            url_in = re.search(
                r'(?:data-src|href|src)=["\']?(https?://[^"\'\s>]+)',
                inner, re.IGNORECASE,
            )
            url = url_in.group(1) if url_in else None
            if not url:
                bare = BARE_URL_RE.search(inner)
                url = bare.group(0) if bare else "<embed>"
            matches.append(UrlMatch(
                kind="linkcard",
                url=url,
                snippet=_snippet(html, m.span()),
            ))
            return ""
        html = pattern.sub(repl, html)
    return html


def _strip_bare_urls(html: str, matches: list[UrlMatch]) -> str:
    """<p>...</p> を含むテキスト全域から bare URL を削除。

    属性値内の URL (data-src="..." 等) を誤検出しないため、HTML タグ内部の
    URL を一時的にプレースホルダで隠してから検出 → 復元する 2 段階処理。
    削除後の周辺空白 (連続スペース・空段落) も整理する。
    """
    # 1. 属性値内 URL を退避 (negative lookbehind は可変長で複雑なので置換方式)
    placeholders: list[str] = []

    def stash(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"\x00ATTR_URL_{len(placeholders) - 1}\x00"

    # 属性値 (="...") に含まれる URL を退避
    safe_html = re.sub(r'=(["\'])(https?://[^"\']+)\1', stash, html)
    # data-* / src= / href= の equality なし URL も退避
    safe_html = re.sub(r'(?:data-[a-z-]+|src|href)=(https?://[^\s>]+)', stash, safe_html)

    # 2. テキスト中の bare URL のみ検出
    def repl(m: re.Match[str]) -> str:
        matches.append(UrlMatch(
            kind="bare",
            url=m.group(0),
            snippet=_snippet(html, m.span()),
        ))
        return ""

    out = BARE_URL_RE.sub(repl, safe_html)

    # 3. 退避した属性値 URL を復元 (タグ自体は他段で削除されるため属性のまま残してよい)
    for i, original in enumerate(placeholders):
        out = out.replace(f"\x00ATTR_URL_{i}\x00", original)

    # 4. 削除痕の整理
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"<p\b[^>]*>\s*</p>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def strip_all_urls_from_html(html: str) -> tuple[str, list[UrlMatch]]:
    """note 記事 body HTML から全 URL を削除する。

    順序:
      1. linkcard (先に削除しないと内側の bare URL が誤検出される)
      2. anchor タグ → 中身保持 or self-link なら全削除
      3. bare URL → 削除 + 周辺整理

    Returns:
      (stripped_html, [UrlMatch, ...])  — 検出/削除順を保ったマッチリスト
    """
    matches: list[UrlMatch] = []
    out = _strip_linkcards(html, matches)
    out = _strip_anchors(out, matches)
    out = _strip_bare_urls(out, matches)
    return out, matches


# ---------- レート制限ステートマシン (純関数, テスト容易) ----------

def _today_iso() -> str:
    return date.today().isoformat()


def load_state(path: Path = STATE_PATH) -> dict:
    """``.strip_state.json`` から状態を読み込む。日付が変わっていればカウンタをリセット。"""
    if not path.exists():
        return {"date": _today_iso(), "count": 0, "last_run_ts": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"date": _today_iso(), "count": 0, "last_run_ts": 0.0}
    if data.get("date") != _today_iso():
        return {"date": _today_iso(), "count": 0, "last_run_ts": 0.0}
    return data


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def can_proceed(state: dict, now_ts: float | None = None,
                daily_limit: int = DAILY_LIMIT,
                min_interval: int = MIN_INTERVAL_SECONDS) -> tuple[bool, str]:
    """次の更新を実行してよいか判定。

    Returns:
      (ok, reason)  — ok=False のとき reason に理由を入れる。
    """
    now = now_ts if now_ts is not None else time.time()
    if state.get("count", 0) >= daily_limit:
        return False, f"daily_limit_reached: {state['count']} >= {daily_limit}"
    elapsed = now - float(state.get("last_run_ts", 0.0))
    if state.get("last_run_ts", 0.0) > 0 and elapsed < min_interval:
        wait = int(min_interval - elapsed)
        return False, f"too_soon: 残り {wait}s 待機が必要 (前回から {int(elapsed)}s)"
    return True, "ok"


def record_run(state: dict, now_ts: float | None = None) -> dict:
    state["count"] = state.get("count", 0) + 1
    state["last_run_ts"] = now_ts if now_ts is not None else time.time()
    state["date"] = _today_iso()
    return state


# ---------- バックアップ ----------

def backup_html(key: str, html: str, ts: str | None = None) -> Path:
    """記事 body HTML をタイムスタンプ付きで logs/ に保存。rollback 用。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS_DIR / f"strip_url_backup_{key}_{ts}.html"
    path.write_text(html, encoding="utf-8")
    return path


def append_run_log(line: str, ts: str | None = None) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = ts or datetime.now().strftime("%Y%m%d")
    path = LOGS_DIR / f"strip_url_run_{ts}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


# ---------- 1 記事を処理 (純関数 + I/O) ----------

def analyze_article(meta: dict) -> ArticleStripResult:
    """1 記事を fetch + strip して結果を返す (DB/note 側に書き込まない、純解析)。"""
    key = meta.get("key", "")
    title = meta.get("name", "")
    note_url = f"https://note.com/kaitori_nv_cloud/n/{key}"
    detail = fetch_note_detail(key)
    body_html = detail.get("body", "") or ""
    stripped, matches = strip_all_urls_from_html(body_html)
    return ArticleStripResult(
        key=key, title=title, note_url=note_url,
        original_html=body_html, stripped_html=stripped, matches=matches,
    )


def stripped_html_to_editor_text(stripped_html: str) -> str:
    """URL 削除後の HTML を note エディタに再投入可能なプレーンテキストに変換。

    retrofit._html_to_lines を流用 (連続空行圧縮・段落改行化済)。
    URL は事前の strip_all_urls_from_html で消えているため、リンク化は不要。
    """
    lines = _html_to_lines(stripped_html)
    joined = "\n".join(lines)
    # 連続空行を更に圧縮 (削除痕で発生する3行以上の空白を2行に)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def render_dry_run(result: ArticleStripResult) -> str:
    lines: list[str] = []
    lines.append(f"=== 記事: {result.title or '(無題)'} ({result.note_url}) ===")
    if not result.matches:
        lines.append("削除対象: なし (URL 含まれず)")
        return "\n".join(lines)
    lines.append("削除対象:")
    summary = result.summary()
    by_kind: dict[str, list[UrlMatch]] = {"anchor": [], "self_link": [], "linkcard": [], "bare": []}
    for m in result.matches:
        by_kind[m.kind].append(m)
    for kind, items in by_kind.items():
        if not items:
            continue
        sample = items[0]
        more = f" (他 {len(items) - 1} 箇所)" if len(items) > 1 else ""
        lines.append(f"  - {kind}: \"{sample.snippet}\" → {sample.url}{more}")
    lines.append(f"合計 {result.total_removed} URL 削除予定 (内訳: {summary})")
    return "\n".join(lines)


# ---------- execute モード (Playwright で書き換え) ----------

async def _execute_one_article(
    page,
    result: ArticleStripResult,
    *,
    screenshots_dir: Path | None = None,
) -> dict:
    """1 記事に対する Playwright 書き換え。retrofit.apply_fixes パターンを流用。

    Returns:
      detail dict (key, title, after_url_count, error?)
    Raises:
      RuntimeError on critical failures (caller が即停止すべきもの)
    """
    import platform

    detail: dict = {"key": result.key, "title": result.title}
    text_to_inject = stripped_html_to_editor_text(result.stripped_html)

    edit_url = f"https://editor.note.com/notes/{result.key}/edit"
    await page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
    # editor リダイレクト待ち
    for _ in range(30):
        await page.wait_for_timeout(1000)
        if "/edit" in page.url and "editor.note.com" in page.url:
            break
    await page.wait_for_timeout(4000)

    body_area = page.locator('div[role="textbox"][contenteditable="true"]')
    if await body_area.count() == 0:
        raise RuntimeError("contenteditable body not found")
    await body_area.click()
    await page.wait_for_timeout(500)

    if screenshots_dir is not None:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshots_dir / f"strip_{result.key}_before.png"))

    mod = "Meta" if platform.system() == "Darwin" else "Control"
    await page.keyboard.press(f"{mod}+a")
    await page.wait_for_timeout(300)
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(500)

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
        text_to_inject,
    )
    await page.wait_for_timeout(1500)

    if screenshots_dir is not None:
        await page.screenshot(path=str(screenshots_dir / f"strip_{result.key}_after_input.png"))

    # 2-step save: 「公開に進む」/「更新する」 → ダイアログ → 「更新する」/「投稿する」
    clicked_step1 = False
    for sel in ['button:has-text("公開に進む")', 'button:has-text("更新する")']:
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

    await page.wait_for_timeout(3500)
    if screenshots_dir is not None:
        await page.screenshot(path=str(screenshots_dir / f"strip_{result.key}_dialog.png"))

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

    return detail


async def execute_strip_articles(
    notes: list[dict],
    *,
    state_path: Path = STATE_PATH,
    ignore_rate_limit: bool = False,
    sleep_between: int = MIN_INTERVAL_SECONDS,
) -> dict:
    """選択された記事に対して URL 削除を本実行する。

    安全機構:
      - レート制限チェック (count >= 3 / interval < 30 min) で次記事を中断
      - 各記事の original_html を logs/ にバックアップしてから edit
      - Playwright 失敗時は **即 SystemExit (例外 raise)**、次記事に進まない
      - 成功時は state を update して record_run

    Returns:
      summary dict {processed, skipped_no_match, errors, details: [...]}
    """
    from src.publisher import NotePublisher, SCREENSHOTS_DIR

    summary: dict = {
        "processed": 0,
        "skipped_no_match": 0,
        "skipped_rate_limit": 0,
        "errors": 0,
        "details": [],
    }

    publisher = NotePublisher()
    await publisher.start()
    try:
        context = await publisher._get_context()

        for meta in notes:
            try:
                result = analyze_article(meta)
            except Exception as e:
                append_run_log(f"[execute] key={meta.get('key')} analyze failed: {e}")
                summary["errors"] += 1
                continue

            if not result.matches:
                summary["skipped_no_match"] += 1
                append_run_log(f"[execute] key={result.key} skipped (no URLs)")
                continue

            # レート制限チェック (記事ごと)
            state = load_state(state_path)
            if not ignore_rate_limit:
                ok, reason = can_proceed(state)
                if not ok:
                    append_run_log(f"[execute] key={result.key} rate-limit-stop: {reason}")
                    summary["skipped_rate_limit"] += 1
                    print(f"  ⏸ レート制限により停止: {reason}")
                    break  # 1 日の上限到達 / 待機必要 → 中断

            # バックアップ
            backup_path = backup_html(result.key, result.original_html)
            append_run_log(
                f"[execute] key={result.key} title={result.title!r} "
                f"backup={backup_path.name} planned_removal={result.summary()}"
            )

            # Playwright 書き換え
            page = await context.new_page()
            try:
                detail = await _execute_one_article(
                    page, result, screenshots_dir=SCREENSHOTS_DIR,
                )
                detail["backup"] = str(backup_path)
                detail["removed"] = result.summary()

                # 検証: 再 fetch で URL 残存ゼロを確認
                time.sleep(3)
                post = fetch_note_detail(result.key)
                post_html = post.get("body", "") or ""
                _, post_matches = strip_all_urls_from_html(post_html)
                detail["after_url_count"] = len(post_matches)
                if len(post_matches) > 0:
                    msg = (f"verification failed: {len(post_matches)} URLs remain after edit "
                           f"(key={result.key})")
                    append_run_log(f"[execute] {msg}")
                    print(f"  ⚠ {msg} — 即停止")
                    summary["errors"] += 1
                    summary["details"].append({**detail, "error": msg})
                    raise SystemExit(1)

                summary["processed"] += 1
                summary["details"].append(detail)
                # 成功 → state 更新
                state = record_run(load_state(state_path))
                save_state(state, state_path)
                append_run_log(
                    f"[execute] key={result.key} ✓ saved (after_url_count=0, "
                    f"daily_count={state['count']}/{DAILY_LIMIT})"
                )
                print(f"  ✓ {result.title[:40]} 完了 ({state['count']}/{DAILY_LIMIT} 本)")

                # 次記事まで待機 (1 件目以降のみ。日上限到達で次loopの can_proceed が止める)
                if state["count"] < DAILY_LIMIT and not ignore_rate_limit:
                    print(f"  → 次まで {sleep_between}s 待機 ...")
                    time.sleep(sleep_between)

            except SystemExit:
                raise
            except Exception as e:
                msg = f"playwright error key={result.key}: {e}"
                append_run_log(f"[execute] {msg}")
                print(f"  ✗ {msg} — 即停止")
                summary["errors"] += 1
                summary["details"].append({"key": result.key, "error": str(e)})
                raise SystemExit(1)
            finally:
                await page.close()

        await context.close()
    finally:
        await publisher.stop()
    return summary


# ---------- CLI ----------

def _cli():
    parser = argparse.ArgumentParser(description="note 既存記事から全 URL を一括削除")
    parser.add_argument("--user", default="kaitori_nv_cloud")
    parser.add_argument("--dry-run", action="store_true", help="削除内容を CLI 出力 (デフォルト)")
    parser.add_argument("--execute", action="store_true",
                        help="本実行 (Playwright で書き換え)。凌佳承認後にのみ使用")
    parser.add_argument("--only", default=None, help="特定 1 記事の key のみ対象")
    parser.add_argument("--limit", type=int, default=None,
                        help="dry-run で先頭 N 件のみ処理 (サンプル取得用)")
    parser.add_argument("--ignore-rate-limit", action="store_true",
                        help="レート制限を無視 (デバッグ専用、本実行非推奨)")
    args = parser.parse_args()

    if args.execute and args.dry_run:
        print("error: --execute と --dry-run は併用不可")
        raise SystemExit(2)
    # デフォルトは dry-run (安全側)
    do_dry_run = args.dry_run or not args.execute

    print(f"→ fetching note list for @{args.user} ...")
    notes = fetch_all_notes(args.user)
    print(f"   {len(notes)} 件取得")
    if args.only:
        notes = [n for n in notes if n.get("key") == args.only]
        print(f"   --only {args.only} → {len(notes)} 件")
    if args.limit:
        notes = notes[: args.limit]
        print(f"   --limit {args.limit} → {len(notes)} 件")

    if do_dry_run:
        print("\n=== DRY RUN ===\n")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for meta in notes:
            try:
                result = analyze_article(meta)
            except Exception as e:
                print(f"  ! key={meta.get('key')} 解析失敗: {e}")
                continue
            print(render_dry_run(result))
            print()
            append_run_log(
                f"[dry-run {ts}] key={result.key} title={result.title!r} "
                f"removed={result.total_removed} summary={result.summary()}"
            )
            time.sleep(1.5)
        print("=== 完了 (DRY RUN — note サーバーには書き込んでいません) ===")
        return

    # --execute (Phase 2 — 凌佳承認後にのみ起動すること)
    print("\n=== EXECUTE MODE — Playwright で本文書き換え ===")
    print("⚠ 不可逆操作: 各記事の original_html はバックアップ済 (logs/strip_url_backup_*.html)")
    print(f"レート制限: 30 分間隔 + 1 日 {DAILY_LIMIT} 本上限 (.strip_state.json)\n")

    # 事前チェック: 当日カウント
    state = load_state()
    print(f"今日の処理済: {state.get('count', 0)} / {DAILY_LIMIT}")
    ok_initial, reason = can_proceed(state)
    if not ok_initial and not args.ignore_rate_limit:
        print(f"⏸ レート制限: {reason}")
        print("待機 or 翌日に再実行してください (--ignore-rate-limit でデバッグ起動可)")
        raise SystemExit(2)

    summary = asyncio.run(execute_strip_articles(
        notes,
        ignore_rate_limit=args.ignore_rate_limit,
    ))
    print()
    print(f"=== 完了 ===")
    print(f"  本実行: {summary['processed']} 件")
    print(f"  URL 無しスキップ: {summary['skipped_no_match']} 件")
    print(f"  レート制限スキップ: {summary['skipped_rate_limit']} 件")
    print(f"  エラー: {summary['errors']} 件")
    if summary["errors"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    _cli()
