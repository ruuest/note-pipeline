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
    # X 自動投稿モード: "immediate" | "scheduled" | "none"
    # フォロワーゼロ問題への対策として、生成記事は既定で X スレッド予約 (scheduled).
    # 実際の投稿は X_SHARE_ENABLED=true & .x-session.json 存在が条件 (x_publisher 側でガード)
    x_share_mode: str = "scheduled"
    # x_share_mode="scheduled" の場合の投稿予定（None なら 1時間後）
    x_scheduled_at: datetime | None = None


@dataclass
class PostResult:
    article: Article
    success: bool
    note_url: str | None = None
    error: str | None = None
    posted_at: datetime | None = None
