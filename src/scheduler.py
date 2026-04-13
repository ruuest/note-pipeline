"""投稿スケジューラ — レート制限と投稿ログの管理"""
from __future__ import annotations

import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"

MAX_DAILY_POSTS = int(os.environ.get("MAX_DAILY_POSTS", "3"))
MIN_INTERVAL_MINUTES = int(os.environ.get("MIN_INTERVAL_MINUTES", "30"))
# 同カテゴリ連投ブロック: 直近24hで同カテゴリをこの本数以上投稿していたらブロック
SAME_CATEGORY_MAX_24H = int(os.environ.get("SAME_CATEGORY_MAX_24H", "2"))


def _log_path(d: date | None = None) -> Path:
    d = d or date.today()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"posts_{d.isoformat()}.json"


def _load_log(d: date | None = None) -> list[dict]:
    path = _log_path(d)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_log(entries: list[dict], d: date | None = None):
    path = _log_path(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def get_todays_post_count() -> int:
    entries = _load_log()
    return sum(1 for e in entries if e.get("success", False))


def minutes_until_next_post() -> int:
    entries = _load_log()
    if not entries:
        return 0
    last = entries[-1]
    last_time = datetime.fromisoformat(last["posted_at"])
    elapsed = (datetime.now() - last_time).total_seconds() / 60
    remaining = MIN_INTERVAL_MINUTES - elapsed
    return max(0, int(remaining))


def can_post() -> bool:
    if get_todays_post_count() >= MAX_DAILY_POSTS:
        return False
    if minutes_until_next_post() > 0:
        return False
    return True


def log_post(result):
    """PostResult を今日のログに追記"""
    entries = _load_log()
    entries.append({
        "title": result.article.title,
        "keyword": result.article.keyword,
        "category": getattr(result.article, "category", ""),
        "success": result.success,
        "note_url": result.note_url,
        "error": result.error,
        "posted_at": (result.posted_at or datetime.now()).isoformat(),
    })
    _save_log(entries)


def _entries_last_24h() -> list[dict]:
    """直近24時間の投稿ログを今日+昨日のログファイルから集める"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    cutoff = datetime.now() - timedelta(hours=24)

    merged = _load_log(today) + _load_log(yesterday)
    result = []
    for e in merged:
        if not e.get("success", False):
            continue
        try:
            t = datetime.fromisoformat(e["posted_at"])
        except (KeyError, ValueError):
            continue
        if t >= cutoff:
            result.append(e)
    return result


def last_category_check(category: str | None = None) -> dict:
    """直近24h以内の各カテゴリ投稿数を集計。

    Args:
        category: 指定すれば該当カテゴリの件数を ``count`` に入れて返す。

    Returns:
        {"counts": {category: int}, "count": int, "blocked": bool}
    """
    entries = _entries_last_24h()
    counts: dict[str, int] = {}
    for e in entries:
        cat = e.get("category") or "unknown"
        counts[cat] = counts.get(cat, 0) + 1

    result: dict = {"counts": counts}
    if category is not None:
        c = counts.get(category, 0)
        result["count"] = c
        result["blocked"] = c >= SAME_CATEGORY_MAX_24H
    return result


def can_post_category_safe(next_category: str | None = None) -> bool:
    """次投稿予定のカテゴリが同カテゴリ連投ブロックに触れないか。

    next_category 未指定の場合は、全カテゴリで上限に達していなければ許可。
    """
    info = last_category_check(next_category)
    if next_category is None:
        return all(v < SAME_CATEGORY_MAX_24H for v in info["counts"].values()) or not info["counts"]
    return not info["blocked"]


def generate_daily_summary(d: date | None = None) -> str:
    """当日の投稿サマリをプレーンテキストで生成。Telegram送信用。"""
    d = d or date.today()
    entries = _load_log(d)
    successful = [e for e in entries if e.get("success", False)]
    failed = [e for e in entries if not e.get("success", False)]

    lines = []
    lines.append(f"📅 {d.isoformat()} note投稿サマリ")
    lines.append(f"  成功: {len(successful)}件  失敗: {len(failed)}件")

    cat_counts: dict[str, int] = {}
    for e in successful:
        cat = e.get("category") or "unknown"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    if cat_counts:
        cat_str = "  ".join(f"{k}:{v}" for k, v in cat_counts.items())
        lines.append(f"  カテゴリ: {cat_str}")

    if successful:
        lines.append("")
        lines.append("投稿記事:")
        for e in successful:
            t = e.get("posted_at", "")[11:16]
            title = e.get("title", "(no title)")
            url = e.get("note_url") or ""
            lines.append(f"  {t} {title}")
            if url:
                lines.append(f"    {url}")

    if failed:
        lines.append("")
        lines.append("失敗記事:")
        for e in failed:
            title = e.get("title", "(no title)")
            err = (e.get("error") or "")[:80]
            lines.append(f"  ✗ {title} — {err}")

    return "\n".join(lines)


def get_status() -> dict:
    entries = _load_log()
    successful = sum(1 for e in entries if e.get("success", False))
    failed = sum(1 for e in entries if not e.get("success", False))
    return {
        "date": date.today().isoformat(),
        "total": len(entries),
        "successful": successful,
        "failed": failed,
        "remaining": MAX_DAILY_POSTS - successful,
        "minutes_until_next": minutes_until_next_post(),
        "can_post_now": can_post(),
    }
