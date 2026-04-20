# note-pipeline

note / X (Twitter) 向け記事生成・投稿自動化パイプライン。

## セットアップ

1. 依存インストール
   ```bash
   pip install -r requirements.txt
   ```
2. 環境変数ファイル作成
   ```bash
   cp .env.example .env
   ```
3. `.env` の値を埋める（詳細は下記）

## 環境変数

| 変数 | 用途 |
|---|---|
| `NOTE_EMAIL` / `NOTE_PASSWORD` | note ログイン資格情報 |
| `ANTHROPIC_API_KEY` | Claude API（本文生成） |
| `LP_URL` | 記事末尾に挿入する LP URL (デフォルト: `https://nvcloud-lp.pages.dev/`) |
| `X_BEARER_TOKEN` | X API v2 App-only Bearer Token（読み取り系用） |
| `X_API_KEY` / `X_API_SECRET` | OAuth 1.0a Consumer Key/Secret |
| `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | OAuth 1.0a User Access Token/Secret |

---

## X API 認証セットアップ

### なぜ OAuth 1.0a User Context が必要か

X API v2 では **読み取り系** (users lookup, tweets lookup) は App-only Bearer で
十分だが、**書き込み系** (POST /2/tweets) は `User Context` が必須。Bearer だけ
では 401/403 で拒否される。結果として **5 項目すべて** が必要。

### 取得手順

1. **Developer Portal にアクセス**
   https://developer.x.com/en/portal/dashboard
   (NV CLOUD アカウント `northvalue.assets@gmail.com` でログイン)

2. **Free tier プロジェクトを作成**
   - `Projects & Apps` → `Overview`
   - Free tier 自動付与 (月 1,500 posts / 100 reads / Project 1つ)
   - プロジェクト名: `nv-note-pipeline` 等任意

3. **App を作成**
   - `Add App` → App 名入力
   - App permissions を **Read and write** に設定
     (デフォルトは Read only — これだと投稿できない)
   - App type: **Web App, Automated App or Bot**
   - Callback URI: `http://localhost/` (投稿用なので未使用で可)

4. **Keys and tokens タブから 5 項目取得**
   - `Bearer Token` (Regenerate) → `X_BEARER_TOKEN`
   - `API Key and Secret` (Regenerate) → `X_API_KEY` / `X_API_SECRET`
   - `Access Token and Secret` (Generate, User Context)
     → `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET`
   - ※ 各キーは **生成直後の 1 回しか表示されない**。必ずコピーして `.env` に保存。

5. **`.env` に貼り付け**
   ```bash
   # .env を TextEdit 等で開いて値を埋める
   open -a TextEdit /Users/apple/NorthValueAsset/note-pipeline/.env
   ```

6. **疎通確認**
   ```bash
   bash scripts/x_auth_check.sh
   ```
   すべて OK なら環境変数セットアップ完了。

### Free tier 制約 (2026-04 時点)

| 項目 | 月次上限 |
|---|---|
| Post tweets (POST /2/tweets) | **1,500 / month** |
| Read tweets (GET /2/tweets, lookup) | 100 / month |
| User lookup (GET /2/users/me) | 500 / month |
| Projects | 1 |
| Apps | 1 / project |

※ 超過時は 429 Too Many Requests、翌月 1 日リセット。

### トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `401 Unauthorized` (投稿時) | App permissions が Read only。Developer Portal で Read and write に変更 → **Access Token を再発行** (権限変更は再発行で反映) |
| `403 Forbidden` (投稿時) | User Context が揃っていない。5 項目すべて `.env` にあるか `scripts/x_auth_check.sh` で確認 |
| `429 Too Many Requests` | Free tier 月次上限到達。翌月 1 日まで待機、もしくは Basic ($100/月) 検討 |
| `x_auth_check.sh` が `no .env` | `.env` 未作成。`cp .env.example .env` 後に値を埋める |
| 疎通確認で 200 だが投稿が 403 | Access Token が Read only 時代のもの。Developer Portal で再発行 |

### セキュリティ

- `.env` は `.gitignore` 済み。絶対にコミットしない
- キーの値は Claude / AI に直接見せない。`.env` を開く時は TextEdit 等ローカルで
- `scripts/x_auth_check.sh` は値の **存在有無のみ** 検証、値を画面に出さない
- キー漏洩時は Developer Portal で即 Regenerate

---

## 主要コマンド

```bash
python main.py            # note 記事生成 → 投稿
bash scripts/x_auth_check.sh    # X API 認証疎通確認
bash daily_publish.sh           # 日次投稿
bash scripts/note_daily_summary.sh    # 日次集計
```

---

## X スレッド自動投稿（src/x_publisher.py）

note 投稿成功後、Claude Sonnet 4.6 で 3〜7 ツイートのスレッドを生成し、
**Playwright スクレイピング** で x.com に連投する機能。

> **Phase 1 はスクレイピング方式（.x-session.json）、API 経路（tweepy）は将来のフォールバック**。
> `.env` の `X_BEARER_TOKEN` / `X_API_KEY` 等は記載OK（現状は未使用）。

### 仕様（v2.1 / 2026-04-20、方式: Playwright スクレイピング + ヒューマンライク）
- セッション: `.x-session.json`（`.note-session.json` と別管理）
- フィンガープリント固定: macOS Chrome UA / 1440x900 / ja-JP / Asia/Tokyo
- `playwright-stealth` + `navigator.webdriver` 偽装 で JS 検知を封じる
- フロー: session 復元 → home でスクロール滞在 → 30%確率で TL 閲覧 → Post ダイアログ → 1文字ずつタイピング → "+" で枠追加 → "Post all" → プロフィール滞在
- タイピング: 40〜180 ms/char + 句読点後 200〜600 ms + 5%確率で 500〜1500 ms の迷いポーズ
- 固定 sleep 全廃（全てランダムジッター、`logs/x_timing_*.log` に記録）
- マウス: ベジェ風 3〜5 ステップ迂回 → 50〜200 ms ホバー → down/up 分解
- 投稿時刻ジッター: スケジューラ予約時に ±20 分、同一日は 2 時間以上間隔
- レート制限検知: 30〜60 分ランダム後リトライ
- 3 連続失敗: 24 時間クールダウン（`.x-cooldown.lock`）
- セレクタは多段 fallback（`aria-label`/`data-testid`/`text`）
- 失敗時は `logs/screenshots/x_*.png` に自動保存 + Telegram 通知
- システムプロンプト: `docs/x_thread_prompt.md`（プロダクト大臣作成）
- トーンガイド: `docs/x_tone_guide.md`
- コンプラルール: `config/compliance_rules.yaml`（CR001-050、景表法/古物営業法/ステマ/X TOS/Meta TOS）
- LP URL: `https://nvcloud-lp.pages.dev/`（他URL使用禁止）
- 制約: 各ツイート135字以内、絵文字1ツイート2個まで、CTAは末尾1箇所のみ
- 再生成: バリデーションNG時は最大3回再生成 → 全NGなら `queue/x_approval_queue.json` に積む

### 所要時間（モンテカルロ 1000 試行）

| 投稿形態 | 平均 | p5/p95 | per-tweet avg |
|---|---|---|---|
| 1 ツイート単発 | 66.7 s | 57 / 76 s | 66.7 s |
| 3 本スレッド | 105.6 s | — | 35.2 s |
| 5 本スレッド | 143.9 s | — | 28.8 s |

target 40〜90 s/tweet を単発投稿で満たす。スレッドは共通オーバーヘッド（warmup/post_dwell）が
償却されて per-tweet が下がる（想定どおり）。

### X セッション初期化（手動、最初の1回のみ）

```bash
./scripts/x_auth_init.sh
```

動作:
1. Chromium が `headless=False` で起動
2. `https://x.com/login` が開く
3. ブラウザで X にログイン（2要素認証可）
4. ログイン完了を自動検知（`/home` 遷移、最大 10 分）
5. `.x-session.json` が保存される

以降、cron 投稿はこのセッションを自動で復元。`/login` にリダイレクトされた場合は
Telegram で通知が来るので再度このスクリプトを実行する。

### Article メタデータでの制御

`Article.x_share_mode` で挙動切替:

| 値 | 動作 |
|---|---|
| `"none"` (デフォルト) | X投稿しない |
| `"immediate"` | note投稿成功直後に Xスレッドを生成・投稿 |
| `"scheduled"` | `queue/x_posts.json` に予約登録（`x_scheduled_at` 未指定なら 1時間後） |

### 必要な環境変数

```env
# 必須
X_SHARE_ENABLED=true
ANTHROPIC_API_KEY=sk-ant-...

# optional（cron で headless 実行する場合のみ true に。UI変動リスクあり要検討）
X_HEADLESS=false

# 将来のフォールバック用（現状は未使用、記載OK）
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_TOKEN_SECRET=...
```

`X_SHARE_ENABLED=false` または `.x-session.json` 未作成時はスキップ。

### dry_run（投稿せずプレビュー）

```bash
.venv/bin/python -c "
from src.models import Article
from src.x_publisher import create_thread
article = Article(title='査定工数の課題', body='本文', keyword='工数', theme='工数削減',
                  category='pain', template_id='t1')
result = create_thread(article, axis='A', thread_length=5, dry_run=True)
import json; print(json.dumps(result, ensure_ascii=False, indent=2))
"
```

### スケジューラ統合

`src/scheduler.py::process_x_queue()` を cron / launchd から定期実行:

```bash
*/5 * * * * cd /Users/apple/NorthValueAsset/note-pipeline && \
  .venv/bin/python -c 'from src.scheduler import process_x_queue; print(process_x_queue())'
```

### テスト

```bash
.venv/bin/python -m pytest tests/test_x_publisher.py -v
```

ライブブラウザは起動しない。バリデーション・JSON解析・キュー操作・dry_run・
Playwright 経路モック（session欠落/成功/失敗/disabled）・XSessionError をカバー（29件）。
ライブ投稿テストは `scripts/x_auth_init.sh` で `.x-session.json` を作成した後に手動で実施。
