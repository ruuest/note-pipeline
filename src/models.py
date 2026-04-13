from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Article:
    title: str
    body: str
    keyword: str
    theme: str
    category: str
    template_id: str
    generated_at: datetime = field(default_factory=datetime.now)
    image_path: Path | None = None


@dataclass
class PostResult:
    article: Article
    success: bool
    note_url: str | None = None
    error: str | None = None
    posted_at: datetime | None = None
