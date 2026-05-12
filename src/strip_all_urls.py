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

    # --execute は Phase 2 (凌佳承認後)
    print("\n=== EXECUTE モードは Phase 2 用、凌佳承認後に実装 ===")
    print("Phase 1 では --dry-run のみ動作確認してください。")
    raise SystemExit(0)


if __name__ == "__main__":
    _cli()
