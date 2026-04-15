"""投稿前品質ゲート。
見出し構造/画像有無(将来対応)/文字数/ハッシュタグを検証し、invalid時はpublishをスキップ。
"""
import re
from dataclasses import dataclass, field

from src.models import Article


MIN_BODY_LENGTH = 1500
MAX_BODY_LENGTH = 6000
MIN_HEADINGS = 3
MIN_HASHTAGS = 3

# experience(短文体験談)テンプレ専用しきい値: 競合分析で562字記事が最高エンゲージだったため
EXPERIENCE_MIN_BODY = 700
EXPERIENCE_MAX_BODY = 2500
EXPERIENCE_MIN_HEADINGS = 0


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = []
        if self.errors:
            lines.append("ERRORS:")
            lines.extend(f"  - {e}" for e in self.errors)
        if self.warnings:
            lines.append("WARNINGS:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines) if lines else "OK"


def count_headings(body: str) -> int:
    bracket_headings = len(re.findall(r"【[^】]+】", body))
    markdown_headings = len(re.findall(r"^#{1,3}\s+\S", body, flags=re.MULTILINE))
    return bracket_headings + markdown_headings


def count_hashtags(body: str) -> int:
    return len(re.findall(r"(?:^|\s)#[^\s#]+", body))


def cta_url_is_embeddable(body: str, url: str = "https://kaitori-saas.onrender.com/lp") -> bool:
    """note のリンクカード生成条件: URLが単独行・前後空行。"""
    if url not in body:
        return False
    # URL が独立した行にあることを確認
    pattern = re.compile(r"(^|\n)\s*" + re.escape(url) + r"\s*(\n|$)")
    if not pattern.search(body):
        return False
    return True


def has_image_marker(body: str) -> bool:
    if re.search(r"!\[[^\]]*\]\([^)]+\)", body):
        return True
    if re.search(r"<img\s", body, flags=re.IGNORECASE):
        return True
    return False


def validate_article(article: Article) -> ValidationResult:
    result = ValidationResult(is_valid=True)

    title = (article.title or "").strip()
    body = article.body or ""

    if not title:
        result.errors.append("タイトルが空です")
    elif len(title) < 10:
        result.errors.append(f"タイトルが短すぎます ({len(title)}文字 < 10)")
    elif len(title) > 60:
        result.warnings.append(f"タイトルが長めです ({len(title)}文字 > 60)")

    is_experience = (article.template_id or "") in ("experience", "confession")
    min_body = EXPERIENCE_MIN_BODY if is_experience else MIN_BODY_LENGTH
    max_body = EXPERIENCE_MAX_BODY if is_experience else MAX_BODY_LENGTH
    min_headings = EXPERIENCE_MIN_HEADINGS if is_experience else MIN_HEADINGS

    body_len = len(body)
    if body_len < min_body:
        result.errors.append(f"本文が短すぎます ({body_len}文字 < {min_body})")
    elif body_len > max_body:
        result.warnings.append(f"本文が長すぎます ({body_len}文字 > {max_body})")

    heading_count = count_headings(body)
    if heading_count < min_headings:
        result.errors.append(
            f"見出しが不足しています ({heading_count}個 < {min_headings})"
        )

    hashtag_count = count_hashtags(body)
    if hashtag_count < MIN_HASHTAGS:
        result.errors.append(
            f"ハッシュタグが不足しています ({hashtag_count}個 < {MIN_HASHTAGS})"
        )

    if not has_image_marker(body):
        result.warnings.append(
            "画像が未挿入です（noteのアイキャッチ/本文画像は現状手動/自動未対応）"
        )

    if not cta_url_is_embeddable(body):
        result.errors.append(
            "CTA URLがリンクカード化されない形式です（URLは単独行・絵文字/記号プレフィックスなしにする）"
        )

    if re.search(r"^\s*#{1,6}\s+\S", body, flags=re.MULTILINE):
        result.errors.append(
            "本文に生Markdown見出し(## / ###)が残っています。【】形式に変換してください"
        )

    if result.errors:
        result.is_valid = False

    return result
