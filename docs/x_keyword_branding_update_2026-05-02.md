# X リプ凸キーワード ブランディング更新 (2026-05-02)

## 背景
天皇判断: 個人ブランディング（特定企業実名）のリスク軽減。
- 元勤務先実名はNDA / 守秘義務 / 過去同僚関係でリスクあり
- 同水準の信頼担保はできる代替表現で再構築

## 変更対象
- `/Users/apple/NorthValueAsset/content/x-viral-reply/config.json`
  （※ 当該リポジトリ外のため、本MDで変更履歴のみ note-pipeline 側に保管）

## 変更内容

### 1. voice 文言変更
- 旧: `元デロイト → 出張買取で独立 → AIで一人法人SaaSを自作する実践者`
- 新: `外資系戦略コンサル経験 → 出張買取で独立 → AIで一人法人SaaSを自作する実践者`

### 2. strict_keywords (personal)
**削除**:
- `デロイト出身` (元勤務先実名)
- `LBMA` (一般客向け検索を弱める)

**追加**:
- 外資コンサル系: `外資系コンサル`, `外資コンサル`, `戦略コンサル`, `経営コンサル`, `スタートアップコンサル`, `戦略ファーム`, `戦略ファーム出身`, `BIG4卒`
- 数字経営者軸: `CAC`, `LTV`, `ユニットエコノミクス`, `月粗利`, `粗利率`
- 業界系絞り込み拡張: `ブランド品買取`, `貴金属買取`

### 3. keyword_categories.escape_consulting
**削除**: `デロイト出身`, `Deloitte`, `BIG4`
**追加**: `外資系コンサル`, `経営コンサル`, `スタートアップコンサル`, `戦略ファーム`, `戦略ファーム出身`, `BIG4卒`, `ベイン`, `Bain`, `Accenture`

※ 残置（他人のファームとして検索利用、自分の bio では言及しない）:
`マッキンゼー`, `McKinsey`, `BCG`, `アクセンチュア`

### 4. keyword_categories.cash_engine（軸1強化: 数字経営者）
**追加**: `MRR`, `ARR`, `CAC`, `LTV`, `ユニットエコノミクス`, `Burn rate`, `バーンレート`, `アンミット`, `粗利`, `月粗利`

### 5. keyword_categories.build_in_public（軸4: 失敗談キーワード）
**追加**: `失敗談`, `ピボット`, `撤退`, `解約率`, `チャーン`, `ローンチ失敗`, `うまくいかなかった`, `事業を畳んだ`, `ボツ案`, `ドロップアウト`, `事業の屍`, `うちもしくじった`

### 6. exclude_keywords (personal)
**追加（情報商材臭/詐欺臭ガード）**:
- `投資詐欺`, `絶対儲かる`, `億り人`
- `無料note宣伝`, `公式LINE`, `LINE登録特典`
- `ファネル`, `仕組み化`, `オートメーション`

### 7. 投稿生成プロンプト
- note-pipeline 内 `git grep -i 'デロイト\|Deloitte'` 結果: **0件**（既に該当言及なし）
- 既存の生成テンプレ・例文に修正必要箇所はなし

## 4軸キーワード再構築のサマリ
| 軸 | カテゴリ | 主要キーワード |
|---|---|---|
| 軸1 数字経営者 | cash_engine | MRR / ARR / CAC / LTV / ユニットエコノミクス / 月粗利 |
| 軸2 AI×実業 | ai_driven_solo, backoffice_ai | Claude活用 / AI駆動開発 / 経理自動化 (既存維持) |
| 軸3 コンサル思考 | escape_consulting | 外資系コンサル / 戦略ファーム (デロイト→置換) |
| 軸4 Build in Public | build_in_public | 失敗談 / ピボット / 撤退 / チャーン (失敗談追加) |

## 検証
- `python -c "import json; json.load(open('config.json'))"` JSON妥当性 OK
- 削除/追加キーワードの assertion test 全 PASS
- バックアップ: `config.json.bak.pre_keyword_branding_20260502_102552`

## 適用ステータス
- x-viral-reply (git管理外): **直接適用済み** 2026-05-02 10:25 JST
- 次回 cron 実行 (default 毎時 / 投稿系 8/12/21時) から新キーワードで動作

## 関連 diff
- 同ディレクトリ `x_keyword_branding_update_2026-05-02.diff` 参照
