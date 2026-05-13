"""setup/profile_strip_urls の純関数テスト。

Playwright execute path はライブブラウザが必要なため、ここでは
strip_urls_from_plain_text / _backup_profile のみカバー。
"""
from __future__ import annotations

from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from setup.profile_strip_urls import strip_urls_from_plain_text, _backup_profile


class TestStripUrlsFromPlainText:
    def test_removes_bare_url(self):
        text = "出張買取DX研究所です https://nvcloud-lp.pages.dev/ よろしく"
        cleaned, removed = strip_urls_from_plain_text(text)
        assert removed == ["https://nvcloud-lp.pages.dev/"]
        assert "https" not in cleaned
        assert "出張買取DX研究所です" in cleaned and "よろしく" in cleaned

    def test_removes_multiple_urls(self):
        text = "LP: https://a.com/ Twitter: https://b.com/c"
        cleaned, removed = strip_urls_from_plain_text(text)
        assert len(removed) == 2
        assert "https" not in cleaned

    def test_no_url_no_change(self):
        text = "URL なしの文章"
        cleaned, removed = strip_urls_from_plain_text(text)
        assert cleaned == "URL なしの文章"
        assert removed == []

    def test_collapses_extra_whitespace(self):
        text = "前  https://x.com/  後"
        cleaned, _ = strip_urls_from_plain_text(text)
        # 連続空白は 1 個に圧縮
        assert "  " not in cleaned

    def test_empty_string(self):
        cleaned, removed = strip_urls_from_plain_text("")
        assert cleaned == ""
        assert removed == []


class TestBackupProfile:
    def test_writes_backup_with_bio_and_name(self, tmp_path, monkeypatch):
        from setup import profile_strip_urls as ps
        monkeypatch.setattr(ps, "LOGS_DIR", tmp_path)
        path = _backup_profile("自己紹介\nhttps://x.com/", "凌佳", ts="20260513_090000")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "凌佳" in content
        assert "自己紹介" in content
        assert "https://x.com/" in content, "バックアップは元 URL を保持"
        assert "20260513_090000" in path.name

    def test_writes_backup_without_name(self, tmp_path, monkeypatch):
        from setup import profile_strip_urls as ps
        monkeypatch.setattr(ps, "LOGS_DIR", tmp_path)
        path = _backup_profile("bio only", None, ts="20260513_091000")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "(unknown)" in content
        assert "bio only" in content
