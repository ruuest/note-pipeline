"""src/strip_all_urls の単体テスト。

純関数 (URL 削除ロジック + レート制限ステートマシン) のみカバー。
note API / Playwright は触らない (live は --dry-run で別途検証)。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strip_all_urls import (
    DAILY_LIMIT,
    MIN_INTERVAL_SECONDS,
    backup_html,
    can_proceed,
    load_state,
    record_run,
    save_state,
    strip_all_urls_from_html,
    stripped_html_to_editor_text,
)


# ---------- URL 削除ロジック ----------

class TestStripAllUrlsFromHtml:
    def test_anchor_keeps_inner_text(self):
        html = '<p>詳細は<a href="https://nvcloud-lp.pages.dev/">こちら</a>から</p>'
        out, matches = strip_all_urls_from_html(html)
        assert "こちら" in out, "アンカーの中身テキストは保持される"
        assert 'href' not in out, "<a> タグは消えている"
        assert len(matches) == 1
        assert matches[0].kind == "anchor"
        assert matches[0].url == "https://nvcloud-lp.pages.dev/"

    def test_self_link_removes_entire_tag(self):
        # inner text が href と同じ → bare URL になるので tag ごと削除
        html = '<p>サイト: <a href="https://example.com/x">https://example.com/x</a></p>'
        out, matches = strip_all_urls_from_html(html)
        assert "example.com" not in out, "self-link は中身も href も削除される"
        kinds = [m.kind for m in matches]
        assert "self_link" in kinds

    def test_bare_url_removed(self):
        html = '<p>こんにちは https://x.com/Rttvx2026 ご覧ください</p>'
        out, matches = strip_all_urls_from_html(html)
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
        out, matches = strip_all_urls_from_html(html)
        assert "<figure" not in out, "リンクカード figure は削除される"
        assert "northvalue-assets" not in out
        assert "本文" in out and "続き" in out, "前後段落は維持"
        kinds = [m.kind for m in matches]
        assert "linkcard" in kinds

    def test_embedly_blockquote_removed(self):
        html = (
            '<blockquote class="embedly-card" data-card-controls="0">'
            '<a href="https://nvcloud-lp.pages.dev/">NV CLOUD LP</a>'
            '</blockquote>'
        )
        out, matches = strip_all_urls_from_html(html)
        assert "embedly-card" not in out
        assert "nvcloud-lp" not in out, "card 内 URL も消える"
        kinds = [m.kind for m in matches]
        assert "linkcard" in kinds

    def test_multiple_anchors(self):
        html = (
            '<p><a href="https://a.com/">A</a> と '
            '<a href="https://b.com/">B</a> と '
            '<a href="https://c.com/">C</a></p>'
        )
        out, matches = strip_all_urls_from_html(html)
        anchor_matches = [m for m in matches if m.kind == "anchor"]
        assert len(anchor_matches) == 3
        assert "A" in out and "B" in out and "C" in out
        assert "href" not in out

    def test_no_urls_no_changes(self):
        html = "<p>普通の段落、URL なし。</p>"
        out, matches = strip_all_urls_from_html(html)
        assert matches == []
        assert "普通の段落" in out

    def test_consecutive_blanks_collapsed(self):
        html = '<p>テキスト  https://x.com/a  もう一つ</p>'
        out, _ = strip_all_urls_from_html(html)
        # 連続空白 (削除痕) は 1 個に圧縮
        assert "  " not in out

    def test_empty_paragraph_after_strip_removed(self):
        html = '<p>https://only-url-in-this-p.com/x</p><p>本文</p>'
        out, _ = strip_all_urls_from_html(html)
        # URL 削除で空段落になった <p></p> は除去される
        assert "<p></p>" not in out
        assert "本文" in out

    def test_anchor_with_attributes(self):
        # target/rel など追加属性付き
        html = '<p><a href="https://x.com/" target="_blank" rel="noopener">リンク</a></p>'
        out, matches = strip_all_urls_from_html(html)
        assert "リンク" in out
        assert len(matches) == 1
        assert matches[0].url == "https://x.com/"

    # ---- 固定ドメインリテラル (https:// 接頭辞なし) — 2026-05-14 追加 ----

    def test_bare_domain_nvcloud_lp_no_protocol(self):
        # 残存パターン: <p>nvcloud-lp.pages.dev</p> (n973 で実際に残っていた)
        html = '<p>詳細は nvcloud-lp.pages.dev を見てください</p>'
        out, matches = strip_all_urls_from_html(html)
        assert "nvcloud-lp.pages.dev" not in out
        assert any(m.kind == "bare" and "nvcloud-lp.pages.dev" in m.url for m in matches)

    def test_bare_domain_app_northvalue_no_protocol(self):
        html = '<p>サインアップ: app.northvalue-assets.net/sign-up</p>'
        out, matches = strip_all_urls_from_html(html)
        assert "app.northvalue-assets.net" not in out
        assert any(m.kind == "bare" and "app.northvalue-assets.net" in m.url for m in matches)

    def test_bare_domain_x_com_handle_no_protocol(self):
        html = '<p>SNS フォロー: x.com/Rttvx2026</p>'
        out, matches = strip_all_urls_from_html(html)
        assert "x.com" not in out
        assert any(m.kind == "bare" and "Rttvx2026" in m.url for m in matches)

    def test_bare_domain_lit_link_no_protocol(self):
        html = '<p>リンク集: lit.link/kaitori_nv_cloud</p>'
        out, matches = strip_all_urls_from_html(html)
        assert "lit.link" not in out
        assert any(m.kind == "bare" and "lit.link/kaitori_nv_cloud" in m.url for m in matches)

    def test_no_double_count_https_then_literal(self):
        # https:// プレフィックス付きは BARE_URL_RE で 1 回だけ検出、
        # 後段の固定ドメインリテラルでは検出されない (既に消えているため)
        html = '<p>こちら https://nvcloud-lp.pages.dev/foo</p>'
        out, matches = strip_all_urls_from_html(html)
        bare_matches = [m for m in matches if m.kind == "bare"]
        assert len(bare_matches) == 1, "重複カウントなし — bare 1 件のみ"
        assert "nvcloud-lp.pages.dev" in bare_matches[0].url
        assert "nvcloud-lp" not in out

    def test_realistic_combined_case(self):
        # dry-run 出力例の想定ケース
        html = """
        <p>NV CLOUD のご紹介です。詳細は
          <a href="https://nvcloud-lp.pages.dev/">こちら</a>から!</p>
        <figure class="embed embed-card">
          <iframe src="https://app.northvalue-assets.net/"></iframe>
        </figure>
        <p>SNS フォロー: https://x.com/Rttvx2026 もしくは
          <a href="https://twitter.com/nvcloud">Twitter</a></p>
        """
        out, matches = strip_all_urls_from_html(html)
        summary = {"anchor": 0, "self_link": 0, "linkcard": 0, "bare": 0}
        for m in matches:
            summary[m.kind] += 1
        # anchor 2 (こちら, Twitter), linkcard 1 (figure), bare 1 (x.com)
        assert summary["anchor"] == 2
        assert summary["linkcard"] == 1
        assert summary["bare"] == 1
        assert "こちら" in out and "Twitter" in out
        assert "nvcloud-lp" not in out
        assert "northvalue-assets" not in out
        assert "x.com" not in out


# ---------- レート制限ステートマシン ----------

class TestRateLimitStateMachine:
    def test_can_proceed_when_state_empty(self):
        state = {"date": "2026-05-12", "count": 0, "last_run_ts": 0.0}
        ok, reason = can_proceed(state, now_ts=time.time())
        assert ok, reason

    def test_blocks_at_daily_limit(self):
        state = {"date": "2026-05-12", "count": DAILY_LIMIT, "last_run_ts": 0.0}
        ok, reason = can_proceed(state, now_ts=time.time())
        assert not ok
        assert "daily_limit_reached" in reason

    def test_blocks_when_too_soon(self):
        now = 1_000_000.0
        # 前回が 10 分前 (30 分閾値未満)
        state = {"date": "2026-05-12", "count": 1, "last_run_ts": now - 600}
        ok, reason = can_proceed(state, now_ts=now)
        assert not ok
        assert "too_soon" in reason

    def test_allows_after_interval(self):
        now = 1_000_000.0
        state = {"date": "2026-05-12", "count": 1, "last_run_ts": now - MIN_INTERVAL_SECONDS - 1}
        ok, _ = can_proceed(state, now_ts=now)
        assert ok

    def test_record_run_increments(self):
        now = 1_000_000.0
        state = {"date": "2026-05-12", "count": 1, "last_run_ts": now - 9999}
        new_state = record_run(state, now_ts=now)
        assert new_state["count"] == 2
        assert new_state["last_run_ts"] == now

    def test_load_state_resets_on_new_day(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"date": "2025-01-01", "count": 999, "last_run_ts": 0.0}))
        state = load_state(path)
        assert state["count"] == 0, "日付変わったらカウンタゼロリセット"

    def test_load_state_missing_file_returns_default(self, tmp_path):
        path = tmp_path / "missing.json"
        state = load_state(path)
        assert state["count"] == 0
        assert state["last_run_ts"] == 0.0

    def test_load_state_corrupt_file_returns_default(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{ not valid json")
        state = load_state(path)
        assert state["count"] == 0

    def test_save_then_load_round_trip(self, tmp_path):
        path = tmp_path / "rt.json"
        original = {"date": __import__("datetime").date.today().isoformat(), "count": 2, "last_run_ts": 1234.5}
        save_state(original, path)
        loaded = load_state(path)
        assert loaded["count"] == 2
        assert loaded["last_run_ts"] == 1234.5


class TestBackup:
    def test_backup_html_writes_file(self, tmp_path, monkeypatch):
        # logs ディレクトリを tmp に差し替え
        from src import strip_all_urls as sm
        monkeypatch.setattr(sm, "LOGS_DIR", tmp_path)
        path = backup_html("nXXX", "<p>本文</p>", ts="20260512_120000")
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "<p>本文</p>"
        assert "nXXX" in path.name
        assert "20260512_120000" in path.name


class TestStrippedHtmlToEditorText:
    """URL 削除後の HTML → エディタ再投入用プレーンテキスト変換"""

    def test_paragraphs_to_lines(self):
        html = "<p>段落1</p><p>段落2</p>"
        out = stripped_html_to_editor_text(html)
        assert "段落1" in out
        assert "段落2" in out
        # 段落間に改行
        assert out.count("\n") >= 1

    def test_br_becomes_newline(self):
        html = "<p>1行目<br>2行目</p>"
        out = stripped_html_to_editor_text(html)
        assert "1行目" in out and "2行目" in out

    def test_tags_stripped(self):
        html = "<p>本文<strong>強調</strong>続き</p>"
        out = stripped_html_to_editor_text(html)
        assert "<strong>" not in out
        assert "本文強調続き" in out or ("本文" in out and "強調" in out)

    def test_html_entities_unescaped(self):
        html = "<p>A &amp; B</p>"
        out = stripped_html_to_editor_text(html)
        assert "A & B" in out
        assert "&amp;" not in out

    def test_consecutive_blanks_collapsed(self):
        html = "<p>A</p><p></p><p></p><p></p><p>B</p>"
        out = stripped_html_to_editor_text(html)
        # 3 行以上の空行は 2 行に圧縮
        assert "\n\n\n\n" not in out

    def test_post_url_strip_pipeline(self):
        """strip_all_urls_from_html → stripped_html_to_editor_text の連続パイプライン"""
        html = '<p>こんにちは <a href="https://example.com/">こちら</a>から</p>'
        stripped, _ = strip_all_urls_from_html(html)
        text = stripped_html_to_editor_text(stripped)
        assert "こんにちは" in text
        assert "こちら" in text
        assert "から" in text
        assert "https" not in text, "URL が残っていない"
        assert "<a" not in text and "href" not in text, "<a> タグが残っていない"
