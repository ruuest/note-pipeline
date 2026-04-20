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
tweepy 経由で X に連投する機能。

### 仕様（v1.0 / 2026-04-20）
- システムプロンプト: `docs/x_thread_prompt.md`（プロダクト大臣作成）
- トーンガイド: `docs/x_tone_guide.md`
- コンプラルール: `config/compliance_rules.yaml`（CR001-050、景表法/古物営業法/ステマ/X TOS/Meta TOS）
- LP URL: `https://nvcloud-lp.pages.dev/`（他URL使用禁止）
- 制約: 各ツイート135字以内、絵文字1ツイート2個まで、CTAは末尾1箇所のみ
- 再生成: バリデーションNG時は最大3回再生成 → 全NGなら `queue/x_approval_queue.json` に積む

### Article メタデータでの制御

`Article.x_share_mode` で挙動切替:

| 値 | 動作 |
|---|---|
| `"none"` (デフォルト) | X投稿しない |
| `"immediate"` | note投稿成功直後に Xスレッドを生成・投稿 |
| `"scheduled"` | `queue/x_posts.json` に予約登録（`x_scheduled_at` 未指定なら 1時間後） |

### 必要な環境変数

```env
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_TOKEN_SECRET=...
X_SHARE_ENABLED=true
ANTHROPIC_API_KEY=sk-ant-...
```

`X_SHARE_ENABLED=false` または認証情報欠落時はスキップ（dry_run と同じ振る舞い）。

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

ライブAPIは叩かない。バリデーション・JSON解析・キュー操作・dry_runをカバー（23件）。
ライブ投稿テストは天皇による環境変数投入後に別途実施（次タスク予定）。
