#!/bin/bash
# note 既存記事 遡及スキャン+修正ツール
#
# Usage:
#   ./scripts/note_articles_fix.sh                          # スキャン+レポート出力のみ
#   ./scripts/note_articles_fix.sh --dry-run                # 修正対象+差分プレビュー
#   ./scripts/note_articles_fix.sh --apply                  # 全対象を本文書き替え
#   ./scripts/note_articles_fix.sh --apply --only <key>     # 1記事のみ書き替え
#
# レポート: /Users/apple/NorthValueAsset/cabinet/projects/note_analytics/retrofit_report.md
# 使い方詳細: docs/retrofit_usage.md

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PIPELINE_ROOT" || { echo "cd failed"; exit 1; }

MODE="scan"
ONLY_KEY=""
i=1
while [ $i -le $# ]; do
  arg="${!i}"
  case "$arg" in
    --dry-run) MODE="dry-run" ;;
    --apply)   MODE="apply" ;;
    --only)
      i=$((i + 1))
      ONLY_KEY="${!i}"
      ;;
    --help|-h)
      sed -n '1,12p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 1
      ;;
  esac
  i=$((i + 1))
done

ONLY_ARG=()
if [ -n "$ONLY_KEY" ]; then
  ONLY_ARG=(--only "$ONLY_KEY")
  echo "note 遡及修正ツール (mode=$MODE, only=$ONLY_KEY)"
else
  echo "note 遡及修正ツール (mode=$MODE)"
fi

case "$MODE" in
  scan)
    python3 -m src.retrofit --user kaitori_nv_cloud "${ONLY_ARG[@]}"
    ;;
  dry-run)
    python3 -m src.retrofit --user kaitori_nv_cloud --dry-run "${ONLY_ARG[@]}"
    ;;
  apply)
    python3 -m src.retrofit --user kaitori_nv_cloud --apply "${ONLY_ARG[@]}"
    ;;
esac
