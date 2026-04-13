# 既存note記事 遡及修正ツール (retrofit) 使い方

`src/retrofit.py` + `scripts/note_articles_fix.sh` による、既存記事の
問題検出 → 本文クリーンアップ → 自動書き替えツールのマニュアル。

## 前提条件

1. **note セッションファイル**: `note-pipeline/.note-session.json` が有効
   - 無ければ `python3 main.py publish` を一度実行して手動ログインで生成
   - 期限切れの場合は自動で削除され、再度手動ログインが求められる
2. **Playwright インストール済み**:
   ```bash
   cd /Users/apple/NorthValueAsset/note-pipeline
   uv sync  # or pip install -e .
   playwright install chromium
   ```
3. **非ヘッドレス実行が必要**: note editor は `headless=True` で正しく動かない。Playwright はウィンドウを開く
4. **Python 3.11+**: `from __future__ import annotations` 使用

## 検出ルール

| コード | 検出内容 | 自動修正 |
|---|---|---|
| RAW_HEADING | `## `/`### ` 等の生markdown見出し | ✅ `【…】` へ変換 |
| POINTER_URL | `👉 https://…` | ✅ `👉` を削除し単独行URLに |
| BARE_URL | `<p>` 内の `<a>` 未ラップURL | ✅ `createLink` でアンカー化（LP URLのみ） |
| EYECATCH_MISSING | アイキャッチなし | ❌ 手動（画像アップロード必要） |
| EXCESSIVE_EMOJI | ✨💎🔥等が5個以上 | ✅ 3個までに切り詰め |

## CLI 使用例

### 1. スキャン + レポート出力（デフォルト）

```bash
cd /Users/apple/NorthValueAsset/note-pipeline
./scripts/note_articles_fix.sh
```

- 全記事をスキャンしレポートを `projects/note_analytics/retrofit_report.md` に出力
- 書き替えは行わない、読み取りのみ

### 2. dry-run（差分プレビュー）

```bash
./scripts/note_articles_fix.sh --dry-run
```

- 対象記事ごとに **BEFORE / AFTER の先頭200字プレビュー**を表示
- これで問題なければ apply に進む

### 3. 単一記事の書き替え（推奨: 最初はこれで検証）

```bash
./scripts/note_articles_fix.sh --apply --only n2c37460a8810
```

- 指定 key の1記事のみ本文を書き替える
- **バックアップは事前に手動で**: スキャン済みレポートに全 key が記載されているため、そこから対象の JSON ダンプを取る（下記「バックアップ手順」参照）

### 4. 全記事一斉書き替え（慎重に）

```bash
./scripts/note_articles_fix.sh --apply
```

- `auto_fixable` フラグが立っている全記事に対して順次書き替え実行
- 失敗があっても他の記事には進む
- **必ず事前に `--apply --only <key>` で1件テストしてから実施**

## バックアップ手順（必須）

書き替え前には必ずバックアップを取る。

```bash
cd /Users/apple/NorthValueAsset/note-pipeline
BACKUP_DIR=/Users/apple/NorthValueAsset/cabinet/projects/note_analytics/backup_$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"
python3 -c "
import json, time
from pathlib import Path
from src.retrofit import fetch_all_notes, fetch_note_detail
backup = Path('$BACKUP_DIR')
for n in fetch_all_notes('kaitori_nv_cloud'):
    key = n['key']
    d = fetch_note_detail(key)
    (backup / f'{key}.json').write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    print(key, 'backed up')
    time.sleep(1.2)
"
echo "backup at $BACKUP_DIR"
```

## ロールバック手順

書き替え後に問題が発覚した場合:

1. バックアップ JSON から該当 key の `body` フィールドを取り出す:
   ```bash
   python3 -c "import json; d=json.load(open('backup_YYYYMMDD_HHMMSS/<key>.json')); print(d['body'])" > /tmp/original.html
   ```
2. `https://editor.note.com/notes/<key>/edit` をブラウザで開く
3. エディタの本文を全選択 → 削除
4. `/tmp/original.html` の内容を参考に手動で元の構造を復元
5. 保存

**注意**: note には記事復元 API がない。バックアップは「何が元だったか」の確認用で、自動書き戻しはできない。重要な書き替えは `--apply --only <key>` で1本ずつ検証しながら進めること。

## 失敗時のトラブルシュート

| 症状 | 原因 | 対処 |
|---|---|---|
| `contenteditable body not found` | エディタ画面未到達 | URL が `/edit` で終わっているか確認、セッション失効の可能性 |
| 手動ログイン画面で止まる | セッション失効 | ブラウザで手動ログイン→自動的に `.note-session.json` 更新 |
| `update button not found` | セレクタ変更 | `screenshots/retrofit_<key>_after_input.png` を見てボタン文字列を確認、`apply_fixes()` のセレクタリストに追加 |
| 検証で `RAW_HEADING` が残る | 再投入時にnote側が markdown 再解釈 | プレビューで確認、必要なら正規表現を強化 |
| `applied_but_dirty` | 本文書き替えは成功したが残存問題あり | そのまま再度 `--apply --only <key>` を実行 / 重度の場合は editor 手動修正 |

## スクリーンショット

書き替え時、以下3枚が `logs/screenshots/` に保存される:

- `retrofit_<key>_before.png` — 編集画面を開いた直後
- `retrofit_<key>_after_input.png` — クリーン版を再投入した直後
- `retrofit_<key>_final.png` — 「更新」ボタン押下後

トラブル時はこれらで状態を確認する。

## 実装ファイル

- `src/retrofit.py` — スキャン・クリーンアップ・書き替え本体
  - `clean_article_body(html) -> str` — HTML→クリーンテキスト
  - `apply_fixes(issues, only_key=None) -> dict` — Playwright書き替え
  - `scan_all(user)` — 全記事スキャン
  - `scan_article(meta)` — 1記事スキャン
- `scripts/note_articles_fix.sh` — CLI ラッパー
- `projects/note_analytics/retrofit_report.md` — スキャンレポート（自動生成）

## 天皇方針

- **一斉自動適用はしない**（`--apply` 単独は上級者向け）
- まず `--apply --only <key>` で1記事ずつ検証
- 重度汚染（RAW_HEADING 9件以上）は削除→再投稿も選択肢
- アイキャッチは本ツールでは扱えない（手動）
