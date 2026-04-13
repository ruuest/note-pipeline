import csv
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path

import os

# Gemini優先、GOOGLE_API_KEY未設定時はAnthropicフォールバック
_USE_GEMINI = bool(os.environ.get("GOOGLE_API_KEY"))
if _USE_GEMINI:
    from google import genai
    from google.genai import types
else:
    import anthropic

from src.models import Article
from src import title_optimizer
from src.thumbnail import generate_thumbnail

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DRAFTS_DIR = BASE_DIR / "drafts"


def load_templates() -> dict:
    with open(CONFIG_DIR / "templates.json", encoding="utf-8") as f:
        return json.load(f)


def load_tags_config() -> dict:
    with open(CONFIG_DIR / "tags.json", encoding="utf-8") as f:
        return json.load(f)


def build_hashtags(keyword: dict, tags_config: dict) -> list[str]:
    fixed = list(tags_config.get("fixed", []))
    max_tags = tags_config.get("max_tags", 5)

    dynamic: list[str] = []

    haystack = " ".join([
        keyword.get("theme", ""),
        keyword.get("main_keyword", ""),
        keyword.get("sub_keywords", ""),
    ])
    for needle, tags in tags_config.get("by_keyword", {}).items():
        if needle in haystack:
            for tag in tags:
                if tag not in fixed and tag not in dynamic:
                    dynamic.append(tag)

    category_map = tags_config.get("by_category", {})
    for tag in category_map.get(keyword.get("category", ""), []):
        if tag not in fixed and tag not in dynamic:
            dynamic.append(tag)

    merged = fixed + dynamic
    return merged[:max_tags]


def format_hashtag_block(tags: list[str]) -> str:
    if not tags:
        return ""
    return "\n\n" + " ".join(f"#{t}" for t in tags) + "\n"


def normalize_markdown_artifacts(text: str) -> str:
    """LLM出力に紛れる生Markdown(見出し/強調/箇条書き)をnote向け表記に正規化。
    Why: noteエディタにそのまま流すと「## まとめ」「### デメリット」が本文に
    残留し、AI生成感が露出する。bracket見出し【...】+ 「・」箇条書きへ変換する。
    """
    out_lines = []
    for line in text.split("\n"):
        # 水平線 (---, ***, ___) は除去
        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            continue
        m = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if m:
            heading = m.group(1).strip()
            # 既に【】で囲まれていたら二重括弧にしない
            if heading.startswith("【") and heading.endswith("】"):
                out_lines.append(heading)
            elif "【" in heading or "】" in heading:
                # 部分的に【】を含む場合はそのまま見出しとして使う
                out_lines.append(heading)
            else:
                heading = re.sub(r"^\[(.+?)\]$", r"\1", heading)
                out_lines.append(f"【{heading}】")
            continue
        line = re.sub(r"^\s*[-*+]\s+", "・", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)", r"\1", line)
        out_lines.append(line)
    return "\n".join(out_lines)


def load_keywords() -> list[dict]:
    rows = []
    with open(CONFIG_DIR / "keywords.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def get_unused_keywords(count: int) -> list[dict]:
    keywords = load_keywords()
    unused = [k for k in keywords if k["used"].strip().lower() == "false"]

    # priority列がある場合は tier1→tier2→tier3 の優先順で選択。tier4は選択しない。
    has_priority = any("priority" in k for k in unused)
    if has_priority:
        tier1 = [k for k in unused if k.get("priority", "").strip() == "tier1"]
        tier2 = [k for k in unused if k.get("priority", "").strip() == "tier2"]
        tier3 = [k for k in unused if k.get("priority", "").strip() == "tier3"]
        # tier4は選択しない

        pool: list[dict] = []
        remaining = count

        # tier1 から最大50%配分（切り上げ）
        tier1_target = min(len(tier1), -(-remaining * 5 // 10))  # ceil(remaining*0.5)
        if tier1:
            take = min(tier1_target, remaining)
            pool += random.sample(tier1, take)
            remaining -= take

        # tier2 から最大30%配分
        tier2_target = min(len(tier2), -(-count * 3 // 10))  # ceil(count*0.3)
        if tier2 and remaining > 0:
            take = min(tier2_target, remaining)
            pool += random.sample(tier2, take)
            remaining -= take

        # tier3 で残りを埋める
        if tier3 and remaining > 0:
            take = min(len(tier3), remaining)
            pool += random.sample(tier3, take)
            remaining -= take

        # まだ足りなければ tier1/tier2 で補完
        if remaining > 0:
            extras = [k for k in tier1 + tier2 if k not in pool]
            if extras:
                take = min(len(extras), remaining)
                pool += random.sample(extras, take)

        return pool[:count]

    # フォールバック: priority列なし → 旧来の全選択
    if len(unused) < count:
        count = len(unused)
    return random.sample(unused, count)


def mark_keyword_used(theme: str):
    csv_path = CONFIG_DIR / "keywords.csv"
    rows = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row["theme"] == theme:
                row["used"] = "true"
            rows.append(row)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _select_template(templates_data: dict) -> dict:
    """重み付きランダムでテンプレートを選択。template_weightsが未設定なら均等。"""
    templates = templates_data["templates"]
    weights_map = templates_data.get("template_weights", {})
    if weights_map:
        weights = [weights_map.get(t["id"], 1) for t in templates]
        return random.choices(templates, weights=weights, k=1)[0]
    return random.choice(templates)


def generate_article(keyword: dict, templates_data: dict) -> Article:
    template = _select_template(templates_data)

    user_prompt = template["user_prompt"].format(
        theme=keyword["theme"],
        main_keyword=keyword["main_keyword"],
        sub_keywords=keyword["sub_keywords"],
    )

    if _USE_GEMINI:
        client = genai.Client()
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=template["system_prompt"],
                max_output_tokens=4096,
            ),
        )
        raw_text = response.text
    else:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=template["system_prompt"],
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text

    # タイトルと本文を分離（1行目をタイトルとする）
    lines = raw_text.strip().split("\n", 1)
    title = lines[0].strip().strip("#").strip()
    body = lines[1].strip() if len(lines) > 1 else ""

    # タイトル最適化（5型生成+スコアリング）
    title_opt_cfg = templates_data.get("title_optimization", {})
    if title_opt_cfg.get("enabled", False) and _USE_GEMINI:
        body_summary = body[:500]
        try:
            best, _scored = title_optimizer.optimize_title(
                theme=keyword["theme"],
                main_keyword=keyword["main_keyword"],
                sub_keywords=keyword["sub_keywords"],
                body_summary=body_summary,
                client=client,
            )
            if best:
                title = best
        except Exception as e:
            print(f"  ⚠ タイトル最適化失敗、元タイトルを使用: {e}")

    # 生Markdown見出し/箇条書きを正規化（## → 【】、- → ・）
    body = normalize_markdown_artifacts(body)

    # CTA付与（テーマ別cta_mapから選択。cta_blockフォールバックあり）
    cta_map = templates_data.get("cta_map", {})
    if cta_map:
        category = keyword.get("category", "")
        cta_template = cta_map.get(category) or cta_map.get("default", "")
        cta = cta_template.format(lp_url=templates_data["lp_url"])
    else:
        # 旧cta_blockへのフォールバック
        cta = templates_data.get("cta_block", "").format(lp_url=templates_data["lp_url"])
    body = body + cta

    # ハッシュタグ付与（note本文末尾、タグ面/特集面露出のためSEO強化）
    tags_config = load_tags_config()
    hashtags = build_hashtags(keyword, tags_config)
    body = body + format_hashtag_block(hashtags)

    # サムネイル生成（失敗しても記事生成は続行）
    image_path = None
    try:
        safe_kw = keyword["main_keyword"].replace(" ", "_")[:30]
        draft_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_kw}"
        image_path = generate_thumbnail(
            title=title,
            category=keyword["category"],
            draft_id=draft_id,
        )
        print(f"  🖼 サムネイル: {image_path.name}")
    except Exception as e:
        logger.warning(f"サムネイル生成スキップ: {e}")
        print(f"  ⚠ サムネイル生成スキップ: {e}")

    return Article(
        title=title,
        body=body,
        keyword=keyword["main_keyword"],
        theme=keyword["theme"],
        category=keyword["category"],
        template_id=template["id"],
        image_path=image_path,
    )


def save_draft(article: Article) -> Path:
    DRAFTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_keyword = article.keyword.replace(" ", "_")[:30]
    filename = f"{timestamp}_{safe_keyword}.json"
    filepath = DRAFTS_DIR / filename

    data = {
        "title": article.title,
        "body": article.body,
        "keyword": article.keyword,
        "theme": article.theme,
        "category": article.category,
        "template_id": article.template_id,
        "generated_at": article.generated_at.isoformat(),
        "image_path": str(article.image_path) if article.image_path else None,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return filepath


def load_draft(filepath: Path) -> Article:
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)
    image_path_raw = data.get("image_path")
    return Article(
        title=data["title"],
        body=data["body"],
        keyword=data["keyword"],
        theme=data["theme"],
        category=data["category"],
        template_id=data["template_id"],
        generated_at=datetime.fromisoformat(data["generated_at"]),
        image_path=Path(image_path_raw) if image_path_raw else None,
    )


def generate_batch(count: int) -> list[Path]:
    templates_data = load_templates()
    keywords = get_unused_keywords(count)
    drafts = []

    for kw in keywords:
        print(f"生成中: {kw['theme']}...")
        article = generate_article(kw, templates_data)
        path = save_draft(article)
        mark_keyword_used(kw["theme"])
        drafts.append(path)
        print(f"  → {path.name}")

    return drafts
