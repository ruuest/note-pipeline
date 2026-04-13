#!/bin/bash
# note 自動投稿 cron スクリプト
#
# 使い方:
#   ./scripts/note_publish_cron.sh           # 通常実行 (1記事生成+投稿)
#   ./scripts/note_publish_cron.sh dryrun    # 空ログ書き込みのみで終了
#
# crontab 登録例は docs/cron_schedule.md を参照。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CABINET_ROOT="/Users/apple/NorthValueAsset/cabinet"
LOG_DIR="$PIPELINE_ROOT/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/cron_$(date +%Y%m%d).log"
TELEGRAM_NOTIFY="$CABINET_ROOT/scripts/telegram_notify.sh"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

notify_error() {
  local msg="$1"
  log "ERROR: $msg"
  if [ -x "$TELEGRAM_NOTIFY" ]; then
    "$TELEGRAM_NOTIFY" "🚨 note自動投稿エラー: $msg" >/dev/null 2>&1 || true
  fi
}

# dryrun モード: ログファイル生成のみで終了
if [ "${1:-}" = "dryrun" ]; then
  log "DRYRUN: スクリプト起動確認のみ、投稿は行いません"
  echo "dryrun OK: $LOG_FILE"
  exit 0
fi

cd "$PIPELINE_ROOT" || { notify_error "cd $PIPELINE_ROOT failed"; exit 1; }

# python3 は homebrew 版 (3.14, dotenv 等インストール済) を優先
# macOS 標準 /usr/bin/python3 (3.9) は dotenv 未導入 + PEP604 union 型でランタイムエラー
if [ -x "/opt/homebrew/bin/python3" ]; then
  PYTHON=/opt/homebrew/bin/python3
else
  PYTHON=python3
fi

log "====== cron 起動 ======"
log "cwd: $PIPELINE_ROOT"
log "python: $PYTHON"

# 同カテゴリ連投ブロック (scheduler.py 側のロジックを Python 経由で呼ぶ)
$PYTHON -c "
import sys
from src.scheduler import can_post_category_safe, can_post
if not can_post():
    print('can_post=False: 日次/間隔制限');  sys.exit(2)
if not can_post_category_safe():
    print('category_block: 同カテゴリ連投防止');  sys.exit(3)
print('ok')
" >> "$LOG_FILE" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  case "$rc" in
    2) log "SKIP: 日次上限または間隔制限"; exit 0 ;;
    3) log "SKIP: 同カテゴリ連投ブロック"; exit 0 ;;
    *) notify_error "scheduler チェック失敗 (rc=$rc)"; exit 1 ;;
  esac
fi

log "scheduler check passed, running: $PYTHON main.py run 1"
if $PYTHON main.py run 1 >> "$LOG_FILE" 2>&1; then
  log "投稿成功"

  # 直前の投稿情報を取得してTelegram通知
  POST_INFO=$($PYTHON -c "
import json, sys
from pathlib import Path
from datetime import date
p = Path('logs') / f'posts_{date.today().isoformat()}.json'
if not p.exists():
    sys.exit(0)
entries = json.loads(p.read_text())
if not entries:
    sys.exit(0)
last = entries[-1]
if not last.get('success'):
    sys.exit(0)
count = sum(1 for e in entries if e.get('success'))
title = last.get('title', '(no title)')
url = last.get('note_url', '')
cat = last.get('category') or 'unknown'
print(f'✅ note投稿完了 ({count}/5)\n\n[{cat}] {title}\n{url}')
" 2>/dev/null)

  if [ -n "$POST_INFO" ] && [ -x "$TELEGRAM_NOTIFY" ]; then
    "$TELEGRAM_NOTIFY" "$POST_INFO" >/dev/null 2>&1 || true
  fi
else
  rc=$?
  notify_error "main.py run 1 failed (rc=$rc), 詳細は $LOG_FILE"
  exit 1
fi

log "====== cron 終了 ======"
