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
