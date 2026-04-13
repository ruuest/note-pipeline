#!/bin/bash
# note自動投稿 - 毎朝9時に3本投稿
# cronから呼ばれる前提

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/daily_$(date +%Y%m%d).log"

echo "=== note投稿開始: $(date) ===" >> "$LOG_FILE"

cd "$SCRIPT_DIR"

# .envを読み込み（中身は表示しない）
set -a
source .env 2>/dev/null
set +a

# 下書きが足りなければ先に生成
DRAFT_COUNT=$(ls -1 drafts/*.json 2>/dev/null | wc -l | tr -d ' ')
if [ "$DRAFT_COUNT" -lt 3 ]; then
    echo "下書き不足($DRAFT_COUNT本)。3本生成します..." >> "$LOG_FILE"
    python3 main.py generate 3 >> "$LOG_FILE" 2>&1
fi

# 3本投稿（間隔つき）
python3 main.py run 3 >> "$LOG_FILE" 2>&1

echo "=== note投稿完了: $(date) ===" >> "$LOG_FILE"

# 結果をTelegramで通知
STATUS=$(python3 main.py status 2>&1)
CABINET_DIR="/Users/apple/NorthValueAsset/cabinet"
if [ -f "$CABINET_DIR/scripts/inbox_write.sh" ]; then
    bash "$CABINET_DIR/scripts/inbox_write.sh" pm "note日次投稿完了
$STATUS" --from minister_infra --type report 2>/dev/null || true
fi
