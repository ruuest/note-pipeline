#!/bin/bash
# X 単発投稿 cron ラッパー（3スロット運用：8時 / 12時 / 21時）。
# 天皇FB(2026-05-02)反映: 5スロ→3スロ。アカウント力育成優先（フォロワー獲得最優先）。
#
# スロット設計:
#   8時 (morning_talk):  月粗利1000万連載 / モーニングトーク / 数字提示
#   12時 (lunch_light):  ランチタイム軽めコンテンツ / ハウツー
#   21時 (evening_chat): kaitori-saas 開発日記 / 雑談 / フォロワー対話
#
# キューエントリに "category" フィールドを付ければスロット別優先選択。
# マッチ無し時は最古 pending にフォールバック。
#
# 想定 crontab: 0 8,12,21 * * * bash scripts/x_cron_post.sh

set -u

PIPELINE_ROOT="/Users/apple/NorthValueAsset/note-pipeline"
cd "$PIPELINE_ROOT"

HOUR=$(date +%H)
case "$HOUR" in
  08) SLOT="morning_talk" ;;    # 月粗利1000万連載 / 数字提示
  12) SLOT="lunch_light" ;;     # ランチタイム軽め
  21) SLOT="evening_chat" ;;    # kaitori-saas 開発日記 / 雑談
  *)
    # スロット外時刻からの起動はスキップ（cron 多重設定や手動起動の事故防止）
    echo "[x_cron_post] HOUR=$HOUR is not in slot whitelist (8/12/21), skip" >&2
    exit 0
    ;;
esac

exec "$PIPELINE_ROOT/.venv/bin/python3" -m scripts.x_post_from_queue --slot "$SLOT"
