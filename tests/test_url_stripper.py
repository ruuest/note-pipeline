"""src.utils.url_stripper の単体テスト。

純関数のみカバー: HTML / プレーンテキスト両モードで URL が削除されること、
削除イベントが UrlMatch として記録されること、副作用 (空行など) が整理されること。

note API / Playwright は触らない。
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.url_stripper import (  # noqa: E402
    BARE_URL_RE,
    UrlMatch,
    strip_urls_from_html,
    strip_urls_from_text,
    strip_urls,
)


# ---------- HTML モード: 既存記事リライト用 ----------

class TestStripUrlsFromHtml:
    def test_anchor_keeps_inner_text(self):
        html = '<p>詳細は<a href="https://nvcloud-lp.pages.dev/">こちら</a>から</p>'
        out, matches = strip_urls_from_html(html)
        assert "こちら" in out
        assert "href" not in out
        assert len(matches) == 1
        assert matches[0].kind == "anchor"
        assert matches[0].url == "https://nvcloud-lp.pages.dev/"

    def test_self_link_removes_entire_tag(self):
        html = '<p>サイト: <a href="https://example.com/x">https://example.com/x</a></p>'
        out, matches = strip_urls_from_html(html)
        assert "example.com" not in out
        assert any(m.kind == "self_link" for m in matches)

    def test_bare_url_removed(self):
        html = '<p>こんにちは https://x.com/Rttvx2026 ご覧ください</p>'
        out, matches = strip_urls_from_html(html)
        assert "x.com" not in out
        bare = [m for m in matches if m.kind == "bare"]
        assert len(bare) == 1
        assert bare[0].url == "https://x.com/Rttvx2026"

    def test_linkcard_figure_removed(self):
        html = (
            '<p>本文</p>'
            '<figure class="embed embed-card">'
            '  <iframe src="https://app.northvalue-assets.net/"></iframe>'
            '</figure>'
            '<p>続き</p>'
        )
        out, matches = strip_urls_from_html(html)
        assert "<figure" not in out
        assert "northvalue-assets" not in out
        assert "本文" in out and "続き" in out
        assert any(m.kind == "linkcard" for m in matches)


# ---------- text モード: 新規生成パイプライン用 ----------

class TestStripUrlsFromText:
    def test_bare_url_inline_removed(self):
        text = "こんにちは https://example.com/abc 続きの文"
        out, matches = strip_urls_from_text(text)
        assert "example.com" not in out
        assert "こんにちは" in out
        assert "続きの文" in out
        assert len(matches) == 1
        assert matches[0].kind == "bare"
        assert matches[0].url == "https://example.com/abc"

    def test_url_on_own_line_collapsed_to_empty(self):
        """関連記事ブロックの URL 行が削除痕で空行を残しすぎないこと。"""
        text = "・参考記事タイトル\nhttps://note.com/kaitori_nv_cloud/n/abc123\n次の段落"
        out, _ = strip_urls_from_text(text)
        assert "note.com" not in out
        assert "参考記事タイトル" in out
        assert "次の段落" in out
        # 3 連空行以上が残らない
        assert "\n\n\n" not in out

    def test_multiple_urls_all_removed(self):
        text = (
            "LP: https://nvcloud-lp.pages.dev/ から確認\n"
            "App: https://app.northvalue-assets.net/ にもアクセス可\n"
            "Twitter: https://x.com/Rttvx2026 もよろしく"
        )
        out, matches = strip_urls_from_text(text)
        assert "nvcloud-lp" not in out
        assert "northvalue-assets" not in out
        assert "x.com" not in out
        assert len(matches) == 3
        kinds = {m.kind for m in matches}
        assert kinds == {"bare"}

    def test_no_url_text_unchanged(self):
        text = "URL が無い普通の段落。\n2 行目も普通の本文。"
        out, matches = strip_urls_from_text(text)
        assert out == text
        assert matches == []

    def test_empty_input_safe(self):
        out, matches = strip_urls_from_text("")
        assert out == ""
        assert matches == []

    def test_http_and_https_both_caught(self):
        text = "old: http://insecure.example/path new: https://secure.example/path"
        out, matches = strip_urls_from_text(text)
        assert "insecure" not in out
        assert "secure.example" not in out
        assert len(matches) == 2

    def test_url_followed_by_japanese_close_paren(self):
        """全角カッコ閉じ・読点で URL が切れること (既存 BARE_URL_RE 仕様)。"""
        text = "詳細は（https://example.com/x）参照"
        out, matches = strip_urls_from_text(text)
        assert "example.com" not in out
        assert len(matches) == 1

    def test_hashtags_preserved(self):
        """note ハッシュタグ (#xxx) は URL ではないので残す。"""
        text = "本文 https://nvcloud-lp.pages.dev/\n\n#貴金属買取 #出張買取"
        out, matches = strip_urls_from_text(text)
        assert "nvcloud-lp" not in out
        assert "#貴金属買取" in out
        assert "#出張買取" in out

    def test_trailing_whitespace_cleanup(self):
        text = "テキスト   https://example.com/   末尾"
        out, _ = strip_urls_from_text(text)
        # 連続スペースが整理され、URL が抜けた跡で文末がブツ切りにならない
        assert "  " not in out  # 2連以上のスペースなし
        assert "テキスト" in out
        assert "末尾" in out


# ---------- 統合 API ----------

class TestStripUrlsHelper:
    def test_strip_urls_default_text_mode(self):
        out = strip_urls("see https://a.com/x for details")
        assert "a.com" not in out

    def test_strip_urls_html_mode(self):
        out = strip_urls('<p>see <a href="https://a.com">link</a></p>', html=True)
        assert "link" in out
        assert "href" not in out


# ---------- 後方互換: BARE_URL_RE ----------

class TestBareUrlRe:
    def test_matches_basic_https(self):
        assert BARE_URL_RE.search("https://example.com/")
        assert BARE_URL_RE.search("http://example.com/")

    def test_does_not_match_no_scheme(self):
        assert BARE_URL_RE.search("example.com/foo") is None
