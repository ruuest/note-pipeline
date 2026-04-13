#!/bin/bash
# note 日次サマリ通知 (21:00 cron 想定)
#
# 当日の投稿数・URL 一覧を Telegram に送信。
# 使い方:
#   ./scripts/note_daily_summary.sh          # 当日分を通知
#   ./scripts/note_daily_summary.sh dryrun   # 生成のみ、Telegram送信しない

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CABINET_ROOT="/Users/apple/NorthValueAsset/cabinet"
LOG_DIR="$PIPELINE_ROOT/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/summary_$(date +%Y%m%d).log"
TELEGRAM_NOTIFY="$CABINET_ROOT/scripts/telegram_notify.sh"

cd "$PIPELINE_ROOT" || { echo "cd failed" >&2; exit 1; }

# python3 は homebrew 版 (3.14, dotenv 等インストール済) を優先
if [ -x "/opt/homebrew/bin/python3" ]; then
  PYTHON=/opt/homebrew/bin/python3
else
  PYTHON=python3
fi

SUMMARY=$($PYTHON -c "
from src.scheduler import generate_daily_summary
print(generate_daily_summary())
" 2>> "$LOG_FILE")

if [ -z "$SUMMARY" ]; then
  echo "サマリ生成失敗、詳細は $LOG_FILE" >&2
  exit 1
fi

echo "$SUMMARY" >> "$LOG_FILE"

if [ "${1:-}" = "dryrun" ]; then
  echo "$SUMMARY"
  echo "dryrun: Telegram送信はスキップしました"
  exit 0
fi

if [ -x "$TELEGRAM_NOTIFY" ]; then
  "$TELEGRAM_NOTIFY" "📊 note日次サマリ

$SUMMARY" >/dev/null 2>&1 || {
    echo "Telegram送信失敗、詳細は $LOG_FILE" >&2
    exit 1
  }
  echo "sent"
else
  echo "telegram_notify.sh が実行可能でない: $TELEGRAM_NOTIFY" >&2
  exit 1
fi
