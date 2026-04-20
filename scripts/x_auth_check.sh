#!/bin/bash
# x_auth_check.sh — X API v2 認証疎通確認
# 値は一切表示しない。存在確認は行数カウントのみ。
# 使い方: bash scripts/x_auth_check.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${PROJECT_DIR}/.env"

# ─── カラー ───
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
ng()   { printf "  \033[31m✗\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

echo "━━━ X API v2 認証チェック ━━━"

# ─── Step 1: .env 存在確認 ───
echo ""
echo "[1/3] .env ファイル確認"
if [ ! -f "$ENV_FILE" ]; then
  ng ".env が存在しません: $ENV_FILE"
  echo "      cp .env.example .env  で作成し、Developer Portal の値を投入"
  exit 1
fi
ok ".env 発見 ($(wc -l < "$ENV_FILE" | tr -d ' ') 行)"

# ─── Step 2: 5 キーの存在確認（値は一切読まない） ───
echo ""
echo "[2/3] X API 環境変数の存在確認"
REQUIRED_KEYS=(X_BEARER_TOKEN X_API_KEY X_API_SECRET X_ACCESS_TOKEN X_ACCESS_TOKEN_SECRET)
MISSING=0
for key in "${REQUIRED_KEYS[@]}"; do
  # "KEY=..." で "=" の後に 1 文字以上あるかだけ判定 (grep -c)
  count=$(grep -cE "^${key}=.+" "$ENV_FILE" 2>/dev/null) || count=0
  if [ "$count" -ge 1 ]; then
    ok "${key} セット済み"
  else
    ng "${key} 未設定または空"
    MISSING=$((MISSING + 1))
  fi
done

if [ "$MISSING" -gt 0 ]; then
  echo ""
  echo "  → $MISSING 件の環境変数が未設定です。"
  echo "  → Developer Portal から取得:"
  echo "    https://developer.x.com/en/portal/dashboard"
  echo "  → README.md の「X API 認証セットアップ」節を参照"
  exit 2
fi

# ─── Step 3: 疎通確認 (GET /2/users/me with User Context) ───
echo ""
echo "[3/3] X API 疎通確認 (GET /2/users/me)"

# .env を subshell で source して値は環境変数として読み込む (画面には出さない)
# set +x 相当で、値はシェル変数にしか入らない
set -a
# shellcheck disable=SC1090
. "$ENV_FILE" > /dev/null 2>&1
set +a

# OAuth 1.0a User Context で /2/users/me を叩く
# Python の requests_oauthlib を使う (標準装備は稀なので oauthlib がなければ pip)
python3 - "$X_API_KEY" "$X_API_SECRET" "$X_ACCESS_TOKEN" "$X_ACCESS_TOKEN_SECRET" <<'PYEOF' || exit_code=$?
import sys, json
try:
    from requests_oauthlib import OAuth1Session
except ImportError:
    print("  ! requests_oauthlib 未インストール: pip install requests-oauthlib")
    sys.exit(3)

api_key, api_secret, access_token, access_token_secret = sys.argv[1:5]
oauth = OAuth1Session(
    client_key=api_key,
    client_secret=api_secret,
    resource_owner_key=access_token,
    resource_owner_secret=access_token_secret,
)
r = oauth.get("https://api.x.com/2/users/me", timeout=15)
if r.status_code == 200:
    data = r.json().get("data", {})
    print(f"  ✓ HTTP 200 — 認証成功 @{data.get('username','?')} (id={data.get('id','?')})")
    sys.exit(0)
elif r.status_code == 401:
    print(f"  ✗ HTTP 401 — API Key/Secret が不正か、Access Token が無効")
    sys.exit(4)
elif r.status_code == 403:
    print(f"  ✗ HTTP 403 — App permissions が Read only の可能性。Developer Portal で Read and write に変更し Access Token 再発行")
    sys.exit(5)
elif r.status_code == 429:
    print(f"  ! HTTP 429 — Free tier 月次上限到達")
    sys.exit(6)
else:
    print(f"  ✗ HTTP {r.status_code} — 予期せぬエラー")
    try:
        print(f"    {r.json()}")
    except Exception:
        print(f"    {r.text[:200]}")
    sys.exit(7)
PYEOF

case "${exit_code:-0}" in
  0) ok "認証疎通 OK — 投稿可能な状態です" ;;
  3) ng "Python 依存が不足。pip install requests-oauthlib を実行" ;;
  *) ng "疎通失敗 — 上記メッセージを確認" ;;
esac

echo ""
echo "━━━ 完了 ━━━"
exit "${exit_code:-0}"
