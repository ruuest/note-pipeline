# note 自動投稿 cron スケジュール提案

## 前提

note公式の規約上、**自動投稿そのものは禁止されていない**（2026年1月改定 第22版時点）。
ただし、以下は BAN リスクを高める:

- 大量連投（1日10本以上など）
- 同一カテゴリ・同一テーマの連投
- 深夜の機械的な一定間隔投稿
- スパム的な外部リンク誘導
- 低品質なAI生成記事の量産（コミュニティガイドライン違反）

公式には具体的な「N投稿/分」のレート制限は公表されていないため、
**人間の運用者を模したペース**でスケジュールを組む。

## 推奨スケジュール (BAN 回避優先)

### 平日 (月〜金)
| 時刻 | 用途 | 記事数 |
|---|---|---|
| 09:00 | 朝の通勤帯狙い | 1本 |
| 13:00 | 昼休み狙い | 1本 |
| 19:00 | 帰宅後狙い | 1本 |
| 21:00 | 日次サマリ通知 | — |

- **1日最大3本**（`MAX_DAILY_POSTS=3`）
- **最低間隔2時間**（実装側では `MIN_INTERVAL_MINUTES=120` 推奨、デフォルトは30）
- **同カテゴリは24h以内に2本まで**（`SAME_CATEGORY_MAX_24H=2`、scheduler側でブロック）

### 週末 (土日)
| 時刻 | 用途 | 記事数 |
|---|---|---|
| 10:00 | 週末ブランチ帯 | 1本 |
| 20:00 | 日曜夜狙い | 1本 |
| 21:00 | 日次サマリ通知 | — |

平日より少なめに（1日2本）することで、より人間的なペースに近づく。

## crontab 登録コマンド（天皇承認後に手動登録）

```bash
crontab -e
```

で以下を追記:

```cron
# ========== note 自動投稿 ==========
# 環境変数（必要なら）
MAX_DAILY_POSTS=3
MIN_INTERVAL_MINUTES=120
SAME_CATEGORY_MAX_24H=2
PATH=/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin

# 平日: 9:00, 13:00, 19:00
0 9  * * 1-5 /Users/apple/NorthValueAsset/note-pipeline/scripts/note_publish_cron.sh
0 13 * * 1-5 /Users/apple/NorthValueAsset/note-pipeline/scripts/note_publish_cron.sh
0 19 * * 1-5 /Users/apple/NorthValueAsset/note-pipeline/scripts/note_publish_cron.sh

# 週末: 10:00, 20:00
0 10 * * 6,0 /Users/apple/NorthValueAsset/note-pipeline/scripts/note_publish_cron.sh
0 20 * * 6,0 /Users/apple/NorthValueAsset/note-pipeline/scripts/note_publish_cron.sh

# 日次サマリ: 毎日 21:00
0 21 * * * /Users/apple/NorthValueAsset/note-pipeline/scripts/note_daily_summary.sh
```

## ヒューマン化の工夫（追加検討項目）

- **ランダムジッター**: `sleep $((RANDOM % 600))` を cron 実行冒頭に入れて、毎回0〜10分ずらす
  例: `0 9 * * 1-5 sleep $((RANDOM \% 600)) && /path/to/note_publish_cron.sh`
- **祝日スキップ**: 日本の祝日 API を叩いて投稿しない日を設ける
- **悪天候・災害時の一時停止**: ニュース文脈を読む必要があり、現状は手動停止

## 監視

- **cron ログ**: `note-pipeline/logs/cron_YYYYMMDD.log`
- **失敗時**: Telegram にエラー通知が飛ぶ（`cabinet/scripts/telegram_notify.sh` 経由）
- **日次サマリ**: Telegram に投稿数・URL一覧

## dryrun 動作確認

```bash
cd /Users/apple/NorthValueAsset/note-pipeline

# 投稿スクリプト（ログ書き込みのみで終了）
./scripts/note_publish_cron.sh dryrun

# サマリスクリプト（Telegram送信なしで標準出力のみ）
./scripts/note_daily_summary.sh dryrun
```

## スケジューラ拡張API

`src/scheduler.py` に以下が追加されている:

- `last_category_check(category=None) -> dict` — 直近24hのカテゴリ別投稿数
- `can_post_category_safe(next_category=None) -> bool` — 同カテゴリ連投ブロック判定
- `generate_daily_summary(d=None) -> str` — 当日のプレーンテキストサマリ生成

## 参考

- [note ご利用規約](https://terms.help-note.com/hc/ja/articles/44943817565465-note-%E3%81%94%E5%88%A9%E7%94%A8%E8%A6%8F%E7%B4%84) （2026年1月15日 第22版）
- [noteコミュニティガイドライン](https://www.help-note.com/hc/ja/articles/4409925863193)
