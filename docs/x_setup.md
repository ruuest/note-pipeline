# X(Twitter) 連携セットアップ手順

note 投稿後に X に自動シェアする機能のセットアップ。
**天皇の手動操作が必須** の部分を明示しています。

## 1. X Developer Portal でアプリを作成（手動必須）

1. https://developer.twitter.com/ にログイン
2. 「Projects & Apps」→「Create Project」
3. プロジェクト名: `NV CLOUD note自動化`、Use case: `Making a bot`
4. アプリ作成後、以下を取得:
   - API Key
   - API Secret Key
   - Bearer Token（今回は使用しない）

**注意**: 無料プランは月1500投稿まで（2026時点）。月30本なら余裕。

## 2. アクセストークン発行（手動必須）

1. アプリ設定画面 → 「Keys and tokens」
2. 「Access Token and Secret」→ 「Generate」
3. **User authentication settings** で以下を設定:
   - Type of App: `Web App, Automated App or Bot`
   - App permissions: `Read and write`
   - Callback URI: `https://localhost:3000/callback`（使わないがダミーで必須）
4. 以下を取得:
   - Access Token
   - Access Token Secret

## 3. 環境変数に登録（手動必須）

`note-pipeline/.env` に追記:

```env
X_API_KEY=<取得したAPI Key>
X_API_SECRET=<取得したAPI Secret>
X_ACCESS_TOKEN=<取得したAccess Token>
X_ACCESS_TOKEN_SECRET=<取得したAccess Token Secret>
X_SHARE_ENABLED=true
```

## 4. 依存パッケージ追加（コマンド実行可）

```bash
cd /Users/apple/NorthValueAsset/note-pipeline
uv add tweepy
```

## 5. 実装の有効化（AI大臣が対応）

セットアップ完了後、AI大臣が `src/x_integration.py` の `share_to_x()` を tweepy で実装し、
`main.py` の publish フロー完了後にフックする。

## 6. 動作確認

```bash
cd /Users/apple/NorthValueAsset/note-pipeline
python3 -c "
from src.x_integration import XConfig, build_share_text
c = XConfig.from_env()
print('ready:', c.is_ready())
"
```

## 自動化できない部分（人間の操作が必須）

| 項目 | 理由 |
|---|---|
| X Developer Portal アプリ作成 | 本人認証・電話番号認証が必要 |
| Access Token 発行 | OAuth 認証フローがブラウザ必須 |
| User authentication settings 設定 | GUI経由でしか変更不可 |
| 無料プラン制限の承諾 | 利用規約同意が必要 |

AI大臣ができるのは `.env` 読み込み〜 API呼び出し〜エラーハンドリングまで。
