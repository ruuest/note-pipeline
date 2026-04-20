#!/usr/bin/env python3
"""
note自動投稿パイプライン - NV CLOUD SEO集客用

Usage:
    python main.py generate <count>     # 記事を生成してdrafts/に保存
    python main.py publish              # drafts/から1記事投稿
    python main.py run <count>          # 生成→投稿（間隔あり）
    python main.py status               # 今日の投稿状況
    python main.py list-drafts          # 未投稿の下書き一覧
"""

import sys
import time
import random
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from src.generator import generate_batch, load_draft, save_draft, generate_article, get_unused_keywords, load_templates, mark_keyword_used
from src.publisher import publish_article
from src.scheduler import can_post, log_post, get_status, get_todays_post_count, MAX_DAILY_POSTS, minutes_until_next_post, process_x_queue
from src.validator import validate_article

DRAFTS_DIR = BASE_DIR / "drafts"


def cmd_generate(count: int):
    print(f"記事 {count} 本を生成します...")
    paths = generate_batch(count)
    print(f"\n完了: {len(paths)} 本の記事を生成しました")
    for p in paths:
        print(f"  {p.name}")


def cmd_publish():
    if not can_post():
        status = get_status()
        if status["remaining"] <= 0:
            print(f"本日の上限 ({MAX_DAILY_POSTS}本) に到達済み")
        else:
            print(f"次の投稿まで {status['minutes_until_next']} 分待ってください")
        return

    # 最も古い下書きを取得
    drafts = sorted(DRAFTS_DIR.glob("*.json"))
    if not drafts:
        print("投稿する下書きがありません。先に generate を実行してください")
        return

    draft_path = drafts[0]
    article = load_draft(draft_path)
    print(f"投稿中: {article.title}")

    validation = validate_article(article)
    if not validation.is_valid:
        print(f"品質ゲート NG → 投稿スキップ\n{validation.format()}")
        invalid_path = draft_path.with_suffix(".invalid.json")
        draft_path.rename(invalid_path)
        return
    if validation.warnings:
        print(f"品質ゲート 警告あり:\n{validation.format()}")

    result = publish_article(article)
    log_post(result)

    if result.success:
        print(f"投稿成功: {result.note_url}")
        draft_path.unlink()  # 投稿済みの下書きを削除
    else:
        print(f"投稿失敗: {result.error}")
        # 失敗した下書きはリネームして残す
        failed_path = draft_path.with_suffix(".failed.json")
        draft_path.rename(failed_path)


def cmd_run(count: int):
    remaining = MAX_DAILY_POSTS - get_todays_post_count()
    count = min(count, remaining)

    if count <= 0:
        print(f"本日の上限 ({MAX_DAILY_POSTS}本) に到達済み")
        return

    print(f"{count} 本の記事を生成→投稿します")

    # 既存の下書きが足りなければ生成
    existing_drafts = sorted(DRAFTS_DIR.glob("*.json"))
    needed = count - len(existing_drafts)
    if needed > 0:
        print(f"\n下書きが {needed} 本不足。生成します...")
        generate_batch(needed)

    # 投稿ループ
    for i in range(count):
        if not can_post():
            wait_min = minutes_until_next_post()
            if wait_min > 0:
                print(f"\n{wait_min} 分待機中...")
                time.sleep(wait_min * 60)

        drafts = sorted(DRAFTS_DIR.glob("*.json"))
        if not drafts:
            print("下書きがなくなりました")
            break

        draft_path = drafts[0]
        article = load_draft(draft_path)
        print(f"\n[{i+1}/{count}] 投稿中: {article.title}")

        validation = validate_article(article)
        if not validation.is_valid:
            print(f"  品質ゲート NG → スキップ\n{validation.format()}")
            invalid_path = draft_path.with_suffix(".invalid.json")
            draft_path.rename(invalid_path)
            continue
        if validation.warnings:
            print(f"  品質ゲート 警告:\n{validation.format()}")

        result = publish_article(article)
        log_post(result)

        if result.success:
            print(f"  成功: {result.note_url}")
            draft_path.unlink()
        else:
            print(f"  失敗: {result.error}")
            failed_path = draft_path.with_suffix(".failed.json")
            draft_path.rename(failed_path)

        # 次の投稿まで20〜40分のランダム間隔
        if i < count - 1:
            wait = random.randint(20, 40)
            print(f"  次の投稿まで {wait} 分待機...")
            time.sleep(wait * 60)

    print("\n完了!")
    status = get_status()
    print(f"本日の投稿: {status['successful']} 成功 / {status['failed']} 失敗 / 残り {status['remaining']} 本")


def cmd_status():
    status = get_status()
    drafts = list(DRAFTS_DIR.glob("*.json"))
    print(f"日付: {status['date']}")
    print(f"投稿済み: {status['successful']} 成功 / {status['failed']} 失敗")
    print(f"残り枠: {status['remaining']} 本")
    print(f"下書きストック: {len(drafts)} 本")
    if status["can_post_now"]:
        print("状態: 投稿可能")
    else:
        print(f"状態: 待機中（あと {status['minutes_until_next']} 分）")


def cmd_list_drafts():
    drafts = sorted(DRAFTS_DIR.glob("*.json"))
    if not drafts:
        print("下書きなし")
        return
    print(f"下書き: {len(drafts)} 本")
    for d in drafts:
        article = load_draft(d)
        print(f"  [{article.category}] {article.title}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "generate":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        cmd_generate(count)
    elif command == "publish":
        cmd_publish()
    elif command == "run":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        cmd_run(count)
    elif command == "status":
        cmd_status()
    elif command == "list-drafts":
        cmd_list_drafts()
    else:
        print(f"不明なコマンド: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
