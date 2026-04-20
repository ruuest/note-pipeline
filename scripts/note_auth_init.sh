#!/bin/bash
# note セッション初期化スタンドアロンスクリプト
#
# 使い方:
#   ./scripts/note_auth_init.sh
#
# 動作:
#   1. Chromium を headless=False で起動
#   2. note.com/login を開く
#   3. 天皇がブラウザでログイン（Googleログイン可）
#   4. ログイン完了を自動検知（最大10分待機）
#   5. .note-session.json を保存
#
# セッション期限切れ時に cron から Telegram 通知が来たらこれを実行すること。

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PIPELINE_ROOT"

PYTHON="${PYTHON:-python3}"
exec "$PYTHON" -m src.auth_init "$@"
