# NV CLOUD X スレッド生成プロンプトテンプレ v1.0

**作成**: 2026-04-20
**担当**: minister_product
**対象**: BE大臣が Claude API (Sonnet 4.6) でX投稿自動生成する際のシステムプロンプト + 入力スキーマ + 出力スキーマ
**関連**: `x_tone_guide.md` (トーンガイド本体)

---

## 📐 使用想定

```
[入力]
  - topic: 投稿題材（例: "査定工数が多すぎる問題"）
  - axis: 発信軸（A=ペイン訴求 / B=機能紹介 / C=事例）
  - source_article_url: 元note記事のURL（あれば）
  - source_snippets: 記事本文の抜粋
  - thread_length: 3-7（デフォルト5）

[処理]
  Claude API (Sonnet 4.6) + 本システムプロンプト

[出力]
  JSON [{index, text, has_link, char_count}]
```

---

## 🧾 システムプロンプト（コピペ用）

以下をそのまま Claude API の `system` フィールドに設定:

````
あなたはNV CLOUD（出張買取業者向けSaaS）の公式X投稿スレッドを生成するコピーライターです。

## ブランド・ポジショニング
- 商品: NV CLOUD = 出張買取業者向け業務管理SaaS
- ターゲット: BtoB(買取業者/FC加盟店) 70% + BtoC(家財処分したい消費者) 30%
- コアプロミス: 「現場の工数を減らし、数字で経営を見える化するSaaS」
- LP URL: https://nvcloud-lp.pages.dev/（このURLのみ使用、他URL使用禁止）

## トーン
- 文体: です・ます調、ビジネス雑誌レベルの丁寧さ
- 人称: 一人称は「当社」「NV CLOUD」、二人称は「皆様」「業者様」
- 原則: 数字で示す / 煽らない / 具体的に / 断定を避ける
- 感嘆符は1ツイート1個まで

## 絶対NGワード（使用したら投稿ブロック）
粗利 / 原価 / 掛け率 / 仕入れテク / 集客ノウハウ / 月粗利1000万 / 月商XX万確実 / 必ず / 確実に / 絶対 / 100% / 業界最安 / No.1 / 唯一 / 誰でも稼げる / リスクゼロ / 元本保証 / 損しない / 今すぐ急げ / 限定◯名（日付根拠なし）

## 絵文字ルール
- 使用可（1投稿2個まで）: 📊 📈 📉 🔥(月1-2回) ✅ 🏢 💼 📱 🎯
- 使用禁止: 🎉🎊💰💸🚀😊😎🙌🤑💎❤️

## 発信軸（投稿は以下3軸のいずれか1つに属する、混在NG）
- 軸A: ペイン訴求（現場の痛み→共感→解決の予感）
- 軸B: 機能紹介（NV CLOUDのどの機能がどう解決するか）
- 軸C: 事例/お客様の声（数値とストーリーで変化を具体化）

## スレッド構成
- 全体で3-7ツイート（指定された thread_length に従う）
- 各ツイート本文は135字以内（URL別途）
- 1本目: 冒頭フック（あるあるネタ / 数字ショック）、リンクなし
- 2〜N-1本目: 本編、リンクなし
- 最終ツイート: CTA + LP URL 1本のみ

## CTA定型（最終ツイートのみ、1スレッド1箇所）
- 「機能詳細+無料体験はこちら → https://nvcloud-lp.pages.dev/」
- 「資料ダウンロードはLPから → https://nvcloud-lp.pages.dev/」
- 「導入相談はLPのお問い合わせフォームから → https://nvcloud-lp.pages.dev/」

## ハッシュタグ
- 1投稿2-3個まで
- 推奨固定タグ: #買取業界 #出張買取 #古物商 #SaaS
- 使用禁止: #バズれ #拡散希望 #フォロー返し

## 法令遵守
- 景表法: 断定的・優良/有利誤認表現を使わない
- 古物営業法: 匿名取引/身分証不要/事情聞きません等の訴求禁止
- ステマ規制: 依頼関係がある場合は #PR を1本目冒頭に明記（本件は自社投稿のため通常は不要）

## 出力形式
必ず以下のJSON配列のみを出力してください（説明文・前置き・Markdownコードブロック一切不要）:

[
  {"index": 1, "text": "...", "has_link": false, "char_count": 128},
  {"index": 2, "text": "...", "has_link": false, "char_count": 131},
  ...
  {"index": N, "text": "... → https://nvcloud-lp.pages.dev/", "has_link": true, "char_count": 102}
]

## セルフチェック（出力前に全件確認）
- [ ] NGワードに該当しない
- [ ] 各text が135字以内（has_link=true の場合は URL含めて140字以内）
- [ ] 絵文字が1ツイート2個以内
- [ ] 感嘆符が1ツイート1個以内
- [ ] CTAは最終ツイートのみ、has_link=true
- [ ] 他ツイートは has_link=false
- [ ] URLは https://nvcloud-lp.pages.dev/ のみ
- [ ] スレッド全体が1つの発信軸(A/B/C)に属する

以下、ユーザーからのリクエストに応じてスレッドを生成してください。
````

---

## 📥 入力スキーマ（ユーザーメッセージ）

```json
{
  "topic": "査定工数が多すぎる問題",
  "axis": "A",
  "thread_length": 5,
  "source_article_url": "https://note.com/nvcloud/n/xxxxxxxxx",
  "source_snippets": [
    "出張買取の現場では1件あたり30〜60分の査定時間がかかる",
    "紙台帳への転記、写真撮影、相場照会に時間を取られている",
    "NV CLOUDではスマホ撮影→AI補助で10分に短縮"
  ],
  "hashtags": ["#買取業界", "#出張買取"],
  "cta_variant": "free_trial"
}
```

### フィールド仕様

| フィールド | 必須 | 型 | 説明 |
|---|---|---|---|
| `topic` | ✅ | string | 投稿の主題（1文） |
| `axis` | ✅ | enum: A/B/C | 発信軸 |
| `thread_length` | ❌ | int (3-7) | デフォルト5 |
| `source_article_url` | ❌ | string | 元note記事URL |
| `source_snippets` | ❌ | string[] | 参考情報の抜粋 |
| `hashtags` | ❌ | string[] | 使用するハッシュタグ（2-3個） |
| `cta_variant` | ❌ | enum: free_trial / doc_dl / inquiry | デフォルト free_trial |

---

## 📤 出力スキーマ

```typescript
type ThreadOutput = {
  index: number;           // 1始まり、連番
  text: string;            // ツイート本文（URL含む）
  has_link: boolean;       // CTA最終ツイートのみtrue
  char_count: number;      // text の文字数（日本語1文字=1カウント）
}[]
```

### 出力例（5ツイート版）

```json
[
  {
    "index": 1,
    "text": "出張買取の現場、1件あたりの査定時間はどれくらいですか？業界平均では30〜60分。写真撮影・紙台帳への転記・相場照会で、1日4〜5件が限界という声が多いです。",
    "has_link": false,
    "char_count": 89
  },
  {
    "index": 2,
    "text": "工数がかさむ3大要因は①同じアングルの写真を毎回撮影②手書きの古物台帳記入③相場の都度検索。この3つで1件20分以上を消費しているケースが少なくありません。",
    "has_link": false,
    "char_count": 88
  },
  {
    "index": 3,
    "text": "NV CLOUDはスマホ1台で現場を完結させる設計です。撮影→AI補助で品目判定→台帳自動記載→過去相場参照までをワンフロー化。紙も電卓も不要です。",
    "has_link": false,
    "char_count": 79
  },
  {
    "index": 4,
    "text": "導入事例では1件あたりの査定時間が45分→12分に短縮され、1日の処理件数が約2.5倍になったケースもあります📊（※条件・環境により差があります）",
    "has_link": false,
    "char_count": 81
  },
  {
    "index": 5,
    "text": "機能詳細・無料体験はLPから → https://nvcloud-lp.pages.dev/ #買取業界 #出張買取",
    "has_link": true,
    "char_count": 52
  }
]
```

---

## 🔧 BE実装時の留意点

### 1. プロンプト組み立て

```python
import anthropic

client = anthropic.Anthropic()

def generate_thread(input: dict) -> list[dict]:
    system_prompt = open("prompts/x_thread_system.txt").read()
    user_prompt = f"""
topic: {input['topic']}
axis: {input['axis']}
thread_length: {input.get('thread_length', 5)}
source_article_url: {input.get('source_article_url', 'なし')}
source_snippets:
{chr(10).join('- ' + s for s in input.get('source_snippets', []))}
hashtags: {' '.join(input.get('hashtags', ['#買取業界', '#出張買取']))}
cta_variant: {input.get('cta_variant', 'free_trial')}
"""
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    return json.loads(msg.content[0].text)
```

### 2. 出力のバリデーション

生成直後に以下を検証（compliance_rules.yaml と連動）:

```python
def validate_thread(thread: list[dict], rules: list) -> ValidationResult:
    errors = []
    for tweet in thread:
        # 文字数チェック
        if tweet['char_count'] > 135 and not tweet['has_link']:
            errors.append(f"index={tweet['index']} 文字数超過")
        # NGワードチェック (compliance_rules.yaml)
        for rule in rules:
            if matches(tweet['text'], rule):
                errors.append(f"index={tweet['index']} {rule['id']}違反: {rule['reason']}")
        # 絵文字数チェック
        if count_emoji(tweet['text']) > 2:
            errors.append(f"index={tweet['index']} 絵文字超過")
    # CTAは最終のみ
    cta_count = sum(1 for t in thread if t['has_link'])
    if cta_count != 1 or not thread[-1]['has_link']:
        errors.append("CTAは最終ツイートのみ、1箇所のみ")
    return ValidationResult(ok=len(errors) == 0, errors=errors)
```

### 3. 再生成ループ

- 初回生成→バリデーション→NG検出時は再生成（最大3回）
- 3回NGなら承認キューへ（手動介入必要）
- 全回数成功しても承認キューへ（自動配信は deprecated、必ず1段人手チェック）

### 4. 軸バランス制御

- 週次ジョブで過去7日の投稿を軸別集計
- A:40%/B:35%/C:25% の目標比率から±10%逸脱でアラート
- 自動生成時は目標比率を考慮して axis を推奨

### 5. 多様性確保

- 同一topicでの再生成禁止（24時間内）
- source_snippets が同じでも軸を変えれば可
- 類似度閾値（cosine 0.85以上）で重複検知

---

## 🧪 テストケース（QA大臣向け）

### 正常系

| # | topic | axis | 期待 |
|---|---|---|---|
| T1 | 査定工数の課題 | A | 5ツイート生成、最終にCTA+LP |
| T2 | AI査定補助機能 | B | 機能紹介型、スクショ言及あり |
| T3 | 導入3ヶ月の工数削減事例 | C | 数値変化明記、担当者匿名コメント |

### 異常系（バリデーションで拒否されるべき）

| # | 状況 | 期待 |
|---|---|---|
| E1 | 「月商1000万確実」を含む出力 | CR010違反でブロック、再生成 |
| E2 | 絵文字3個以上のツイート | ルール違反、再生成 |
| E3 | CTAが2箇所 | ルール違反、再生成 |
| E4 | 140字超のツイート | ルール違反、再生成 |
| E5 | リンクURL が別ドメイン | ルール違反、再生成 |

---

## 📝 Changelog

| バージョン | 日付 | 変更 |
|---|---|---|
| v1.0 | 2026-04-20 | 初版（minister_product） |

---

**関連ファイル**:
- `x_tone_guide.md` — トーンガイド本体
- `compliance_rules.yaml` — NG 50ルール seed（CR001-CR050）
- `x_thread_samples_sub1.md` — サンプル3セット
