"""URL 検出・削除の共通モジュール。

note 記事から URL を取り除くロジックを 1 箇所に集約する。

公開 API:
  - ``strip_urls_from_html(html)``  : 既存記事の HTML 本文用 (anchor / linkcard / bare URL)
  - ``strip_urls_from_text(text)``  : 新規生成本文 (プレーンテキスト) 用 (bare URL のみ)
  - ``UrlMatch`` : 検出された 1 URL の記録 (kind / url / snippet)
  - ``BARE_URL_RE`` : bare URL 用の共通正規表現

設計方針:
  HTML / plain text の両モードで同じ URL 判定基準を使う。新規生成パイプライン (generator → publisher)
  も既存の記事リライト (strip_all_urls) も、ここから import して同一の URL 集合を消去する。

参考:
  - 既存実装: src/strip_all_urls.py (Phase 1 — 既存記事リライト用)
  - 新規発注: 自動投稿パイプライン (Phase 4 — generator/publisher)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ---------- 共通正規表現 ----------

# <a href="...">テキスト</a> — テキストのみ残す (or self-link は全削除)
A_TAG_RE = re.compile(
    r'<a\b[^>]*\bhref=(?:"[^"]*"|\'[^\']*\'|[^\s>]+)[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

# note のリンクカード — 親要素含めて削除
EMBED_FIGURE_RE = re.compile(
    r'<figure\b[^>]*class=["\'][^"\']*embed[^"\']*["\'][^>]*>.*?</figure>',
    re.DOTALL | re.IGNORECASE,
)
EMBED_TAG_RE = re.compile(r'<embed\b[^>]*/?>', re.IGNORECASE)
EMBED_BLOCKQUOTE_RE = re.compile(
    r'<blockquote\b[^>]*class=["\'][^"\']*embedly-card[^"\']*["\'][^>]*>.*?</blockquote>',
    re.DOTALL | re.IGNORECASE,
)
EMBED_CUSTOM_RE = re.compile(
    r'<(?:external-article|embed-card|note-embed)\b[^>]*>(?:.*?</[^>]+>)?',
    re.DOTALL | re.IGNORECASE,
)

# bare URL (テキスト中に裸で残った http(s)://…)
BARE_URL_RE = re.compile(r'https?://[^\s<>"\'）)、]+', re.IGNORECASE)

# 固定ドメインリテラル (https:// 接頭辞なしのプレーン記載も削除)
# 2026-05-14 minister_be diagnosis: n973f751dec7e に <p>nvcloud-lp.pages.dev</p>
# のようなプレーンドメインが残っていた。NV CLOUD 自社運用専用化のため、
# これらドメインがテキスト中に出てきたら全部削除する。
# strip_all_urls.py (commit 9f60f8d) からの同期 patch (PR #9 共通モジュール側)。
# 順序: BARE_URL_RE (https?://) で先に検出 → このパターンで残り (プロトコルなし) を捕捉。
# 重複カウントなし (前者で消えた URL はもう存在しない)。
BARE_DOMAIN_PATTERNS = [
    re.compile(r'nvcloud-lp\.pages\.dev[/\w\-?=&%.]*', re.IGNORECASE),
    re.compile(r'app\.northvalue-assets\.net[/\w\-?=&%.]*', re.IGNORECASE),
    # x.com/Rttv2026 / x.com/Rttvx2026 など SNS ハンドル
    re.compile(r'x\.com/Rttvx?2026?[/\w\-?=&%.]*', re.IGNORECASE),
    re.compile(r'lit\.link/[\w\-]+', re.IGNORECASE),
]


@dataclass
class UrlMatch:
    """1 つの URL 削除イベントを記録する。"""
    kind: str   # 'anchor' | 'self_link' | 'linkcard' | 'bare'
    url: str    # 削除対象 URL (linkcard 等で取得不能な場合は '<embed>')
    snippet: str  # 周辺テキスト (デバッグ用、最大 80 文字)


# ---------- 内部ヘルパー ----------

def _snippet(src: str, span: tuple[int, int], width: int = 60) -> str:
    start = max(0, span[0] - width // 2)
    end = min(len(src), span[1] + width // 2)
    raw = src[start:end].replace("\n", " ")
    return re.sub(r"\s+", " ", raw)[:80]


def _strip_anchors(html: str, matches: list[UrlMatch]) -> str:
    """<a href='X'>Y</a> → Y (テキストのみ残す)。self-link は全削除。"""
    def repl(m: re.Match[str]) -> str:
        href_match = re.search(r'href=(["\']?)([^"\'\s>]+)\1', m.group(0), re.IGNORECASE)
        href = href_match.group(2) if href_match else ""
        inner = m.group(1) or ""
        inner_text = re.sub(r"<[^>]+>", "", inner).strip()
        kind = "self_link" if inner_text == href and href else "anchor"
        matches.append(UrlMatch(
            kind=kind,
            url=href or "<unknown>",
            snippet=_snippet(html, m.span()),
        ))
        if kind == "self_link":
            return ""
        return inner
    return A_TAG_RE.sub(repl, html)


def _strip_linkcards(html: str, matches: list[UrlMatch]) -> str:
    """note のリンクカードを削除。"""
    for pattern in (EMBED_FIGURE_RE, EMBED_BLOCKQUOTE_RE, EMBED_CUSTOM_RE, EMBED_TAG_RE):
        def repl(m: re.Match[str]) -> str:
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


def _strip_bare_urls_in_html(html: str, matches: list[UrlMatch]) -> str:
    """HTML 文字列から bare URL を削除 (属性値内 URL は退避→復元)。"""
    placeholders: list[str] = []

    def stash(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"\x00ATTR_URL_{len(placeholders) - 1}\x00"

    safe_html = re.sub(r'=(["\'])(https?://[^"\']+)\1', stash, html)
    safe_html = re.sub(r'(?:data-[a-z-]+|src|href)=(https?://[^\s>]+)', stash, safe_html)

    def repl(m: re.Match[str]) -> str:
        matches.append(UrlMatch(
            kind="bare",
            url=m.group(0),
            snippet=_snippet(html, m.span()),
        ))
        return ""

    out = BARE_URL_RE.sub(repl, safe_html)

    # 固定ドメインリテラル (https:// 接頭辞なしのプレーン記載) を追加削除
    # https?://nvcloud-lp.pages.dev 等は前段の BARE_URL_RE で既に消えているため、
    # ここに到達するのはプロトコル無しの記載のみ → 重複カウントなし。
    for pattern in BARE_DOMAIN_PATTERNS:
        out = pattern.sub(repl, out)

    for i, original in enumerate(placeholders):
        out = out.replace(f"\x00ATTR_URL_{i}\x00", original)

    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"<p\b[^>]*>\s*</p>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


# ---------- 公開 API ----------

def strip_urls_from_html(html: str) -> tuple[str, list[UrlMatch]]:
    """note 記事 body HTML から全 URL を削除する。

    順序:
      1. linkcard (先に削除しないと内側の bare URL が誤検出される)
      2. anchor タグ → 中身保持 or self-link なら全削除
      3. bare URL → 削除 + 周辺整理

    Returns:
      (stripped_html, matches)
    """
    matches: list[UrlMatch] = []
    out = _strip_linkcards(html, matches)
    out = _strip_anchors(out, matches)
    out = _strip_bare_urls_in_html(out, matches)
    return out, matches


def strip_urls_from_text(text: str) -> tuple[str, list[UrlMatch]]:
    """プレーンテキストから bare URL を削除する (生成パイプライン用)。

    HTML タグは含まれない前提。bare URL を消去し、削除痕として残る
    空行 / 連続スペースを整理する。

    Returns:
      (stripped_text, matches)
    """
    matches: list[UrlMatch] = []
    if not text:
        return text, matches

    lines_out: list[str] = []
    for line in text.split("\n"):
        # 行内の bare URL を検出して削除
        def repl(m: re.Match[str]) -> str:
            matches.append(UrlMatch(
                kind="bare",
                url=m.group(0),
                snippet=_snippet(line, m.span()),
            ))
            return ""
        new_line = BARE_URL_RE.sub(repl, line)
        # 固定ドメインリテラル (プロトコル無し) も削除 — 同 repl で kind="bare" 記録
        for pattern in BARE_DOMAIN_PATTERNS:
            new_line = pattern.sub(repl, new_line)
        # 行内空白整理
        new_line = re.sub(r"[ \t]+", " ", new_line).strip()
        lines_out.append(new_line)

    out = "\n".join(lines_out)
    # 3 連空行以上 → 2 行に圧縮 (削除痕)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out, matches


def strip_urls(content: str, *, html: bool = False) -> str:
    """シンプル API: 削除後の文字列のみ返す (matches が不要な呼び出し向け)。"""
    stripped, _ = (strip_urls_from_html if html else strip_urls_from_text)(content)
    return stripped
