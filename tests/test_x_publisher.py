"""x_publisher の単体テスト。

ライブAPIは叩かない:
- スレッド分割は generate_thread() を直接呼ばず、validate_thread() / count_emoji() /
  _check_compliance() / _parse_thread_json() / pop_due_entries() を中心にカバー。
- live投稿テストは tests/live/ で別途実施（環境変数あり時のみ）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.x_publisher import (
    ALLOWED_URL,
    EMOJI_PER_TWEET_MAX,
    THREAD_MAX,
    THREAD_MIN,
    TWEET_HARD_LIMIT,
    TWEET_SAFE_LIMIT,
    XQueueEntry,
    _parse_thread_json,
    _strip_url,
    article_to_input,
    count_emoji,
    enqueue,
    list_queue,
    load_compliance_rules,
    load_system_prompt,
    pop_due_entries,
    update_entry,
    validate_thread,
)
from src.models import Article


# ─── helpers ─────────────────────────────────────────
def _ok_thread(n: int = 5) -> list[dict]:
    """検証通過する標準スレッド（n本、最後にCTA）。

    NV CLOUDのSaaS製品紹介を想定した無害な本文。CR016（古物商許可番号必須）に
    抵触しないよう、買取/査定への直接的言及は避ける。
    """
    base = [
        {
            "index": i + 1,
            "text": f"本文{i+1}: 業務管理SaaSの導入で現場の工数を平均30%削減した事例があります。",
            "has_link": False,
            "char_count": 35,
        }
        for i in range(n - 1)
    ]
    cta = {
        "index": n,
        "text": f"機能詳細はLPから → {ALLOWED_URL} #SaaS #古物商",
        "has_link": True,
        "char_count": 25,
    }
    return base + [cta]


# ─── system prompt / compliance rules ────────────────
def test_load_system_prompt_returns_text():
    p = load_system_prompt()
    assert isinstance(p, str)
    assert len(p) > 50  # 何かしらのプロンプトが入っている


def test_load_compliance_rules_has_seed():
    rules = load_compliance_rules()
    assert isinstance(rules, list)
    assert len(rules) >= 15  # CR001-CR015 最低限ある
    ids = {r["id"] for r in rules}
    for required in ["CR001", "CR010"]:
        assert required in ids, f"{required} がルールセットに存在しない"


# ─── 文字数 / 絵文字 ─────────────────────────────────
def test_strip_url():
    assert _strip_url("テキスト https://example.com 続き") == "テキスト  続き"


def test_count_emoji_basic():
    assert count_emoji("ハロー") == 0
    assert count_emoji("📊データ") == 1
    assert count_emoji("📊📈🔥") == 3


# ─── parse JSON ─────────────────────────────────────
def test_parse_thread_json_plain_array():
    raw = json.dumps([
        {"index": 1, "text": "あ", "has_link": False, "char_count": 1},
        {"index": 2, "text": f"い → {ALLOWED_URL}", "has_link": True, "char_count": 1},
    ])
    parsed = _parse_thread_json(raw)
    assert len(parsed) == 2
    assert parsed[0]["index"] == 1
    assert parsed[1]["has_link"] is True
    # char_count はURL除外で再計算される
    assert parsed[1]["char_count"] == len("い → ")


def test_parse_thread_json_with_fence():
    raw = "```json\n" + json.dumps([{"text": "あ"}]) + "\n```"
    parsed = _parse_thread_json(raw)
    assert parsed[0]["text"] == "あ"


def test_parse_thread_json_with_prefix_text():
    raw = "以下が結果です:\n" + json.dumps([{"text": "あ"}]) + "\n以上"
    parsed = _parse_thread_json(raw)
    assert parsed[0]["text"] == "あ"


# ─── validate_thread: 正常系 ────────────────────────
def test_validate_ok_thread_passes():
    res = validate_thread(_ok_thread())
    assert res.ok is True, f"errors: {res.errors}"


# ─── validate_thread: スレッド本数 ────────────────
def test_validate_too_few_tweets():
    res = validate_thread(_ok_thread(n=2))
    assert res.ok is False
    assert any("範囲外" in e for e in res.errors)


def test_validate_too_many_tweets():
    res = validate_thread(_ok_thread(n=8))
    assert res.ok is False
    assert any("範囲外" in e for e in res.errors)


# ─── validate_thread: 文字数超過 ──────────────────
def test_validate_over_safe_limit():
    long_text = "あ" * (TWEET_SAFE_LIMIT + 1)
    thread = _ok_thread()
    thread[0]["text"] = long_text
    res = validate_thread(thread)
    assert res.ok is False
    assert any("上限" in e for e in res.errors)


def test_validate_url_preserves_count():
    """URL を含む本文は、URL を除いた文字数で判定される。"""
    text_with_url = "あ" * 130 + f" → {ALLOWED_URL}"
    thread = _ok_thread()
    thread[-1]["text"] = text_with_url  # 末尾CTAを置換
    res = validate_thread(thread)
    # 130字 < 135字制限なので OK
    assert res.ok is True, f"errors: {res.errors}"


# ─── validate_thread: 絵文字超過 ──────────────────
def test_validate_too_many_emojis():
    thread = _ok_thread()
    thread[0]["text"] = "📊📈🔥多すぎ絵文字"
    res = validate_thread(thread)
    assert res.ok is False
    assert any("絵文字" in e for e in res.errors)


# ─── validate_thread: CTA配置 ────────────────────
def test_validate_no_cta_fails():
    thread = _ok_thread()
    thread[-1]["has_link"] = False
    res = validate_thread(thread)
    assert res.ok is False
    assert any("CTA" in e for e in res.errors)


def test_validate_double_cta_fails():
    thread = _ok_thread()
    thread[0]["has_link"] = True
    thread[0]["text"] = f"前半にCTA → {ALLOWED_URL}"
    res = validate_thread(thread)
    assert res.ok is False
    assert any("CTA" in e for e in res.errors)


def test_validate_cta_not_at_end_fails():
    thread = _ok_thread(n=5)
    # 末尾の has_link を False にして、3本目を CTA にする
    thread[-1]["has_link"] = False
    thread[-1]["text"] = thread[-1]["text"].replace(ALLOWED_URL, "")
    thread[2]["has_link"] = True
    thread[2]["text"] = f"中盤CTA → {ALLOWED_URL}"
    res = validate_thread(thread)
    assert res.ok is False


def test_validate_disallowed_url():
    thread = _ok_thread()
    thread[-1]["text"] = "他URL → https://example.com/"
    res = validate_thread(thread)
    assert res.ok is False
    assert any("許可URL以外" in e for e in res.errors)


# ─── validate_thread: コンプラ規則 ────────────────
def test_validate_blocks_kettoho_certain_profit():
    thread = _ok_thread()
    thread[0]["text"] = "出張買取は確実に儲かるビジネスです"  # CR001
    res = validate_thread(thread)
    assert res.ok is False
    assert any("CR001" in e for e in res.errors)


def test_validate_blocks_monthly_revenue_guarantee():
    thread = _ok_thread()
    thread[0]["text"] = "月収100万確実の出張買取コンサル"  # CR010
    res = validate_thread(thread)
    assert res.ok is False
    assert any("CR010" in e for e in res.errors)


def test_validate_blocks_no_id_check():
    thread = _ok_thread()
    thread[0]["text"] = "身分証明書不要で簡単買取！"  # CR017
    res = validate_thread(thread)
    assert res.ok is False
    assert any("CR017" in e for e in res.errors)


# ─── article_to_input ───────────────────────────────
def test_article_to_input_basic():
    a = Article(
        title="査定工数の課題",
        body="L1\nL2\n\nL3",
        keyword="査定工数",
        theme="工数削減",
        category="pain",
        template_id="t1",
    )
    inp = article_to_input(a, note_url="https://note.com/x", axis="A", thread_length=5)
    assert inp["topic"] == "査定工数の課題"
    assert inp["axis"] == "A"
    assert inp["thread_length"] == 5
    assert inp["source_article_url"] == "https://note.com/x"
    assert inp["source_snippets"] == ["L1", "L2", "L3"]


# ─── キュー操作 ─────────────────────────────────────
def test_enqueue_and_pop_due(tmp_path, monkeypatch):
    qfile = tmp_path / "x_posts.json"
    monkeypatch.setattr("src.x_publisher.QUEUE_FILE", qfile)
    monkeypatch.setattr("src.x_publisher.QUEUE_DIR", tmp_path)

    past = (datetime.now() - timedelta(minutes=5)).isoformat()
    future = (datetime.now() + timedelta(hours=1)).isoformat()
    enqueue(XQueueEntry(scheduled_at=past, article_id="a1", article_title="T1"))
    enqueue(XQueueEntry(scheduled_at=future, article_id="a2", article_title="T2"))

    due = pop_due_entries()
    assert len(due) == 1
    assert due[0]["article_id"] == "a1"

    update_entry("a1", status="posted", tweet_ids=["123"])
    after_update = list_queue(status="posted")
    assert len(after_update) == 1
    assert after_update[0]["tweet_ids"] == ["123"]


# ─── サンプル: dry_run出力チェック（mock） ─────────
def test_create_thread_dry_run_returns_thread_when_credentials_missing(monkeypatch):
    """Claudeが正常に応答した想定でmockし、dry_run=Trueなら投稿せずに thread を返す。"""
    import src.x_publisher as xp

    sample = _ok_thread(5)

    def fake_call_claude(input_data, *, api_key, model=xp.CLAUDE_MODEL):
        return sample

    monkeypatch.setattr(xp, "_call_claude", fake_call_claude)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test_key")

    article = Article(
        title="topic",
        body="本文",
        keyword="kw",
        theme="th",
        category="pain",
        template_id="t",
    )
    cfg = xp.XPublisherConfig(
        api_key="", api_secret="", access_token="", access_token_secret="",
        enabled=False, anthropic_api_key="test_key",
    )
    res = xp.create_thread(article, dry_run=True, config=cfg)
    assert res["success"] is True
    assert res["dry_run"] is True
    assert len(res["thread"]) == 5
    assert res["tweet_ids"] == []
