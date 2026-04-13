"""X(Twitter) 連携スケルトン。
投稿後に note URL+タイトルを X に自動シェアする。
OAuth2 アプリ作成とトークン取得は天皇の手動操作が必須（docs/x_setup.md 参照）。
"""
import os
from dataclasses import dataclass

from src.models import PostResult


@dataclass
class XConfig:
    api_key: str
    api_secret: str
    access_token: str
    access_token_secret: str
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "XConfig":
        return cls(
            api_key=os.environ.get("X_API_KEY", ""),
            api_secret=os.environ.get("X_API_SECRET", ""),
            access_token=os.environ.get("X_ACCESS_TOKEN", ""),
            access_token_secret=os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
            enabled=os.environ.get("X_SHARE_ENABLED", "false").lower() == "true",
        )

    def is_ready(self) -> bool:
        return self.enabled and all(
            [self.api_key, self.api_secret, self.access_token, self.access_token_secret]
        )


def build_share_text(result: PostResult, max_len: int = 280) -> str:
    if not result.success or not result.note_url:
        return ""
    title = result.article.title
    hashtags = " #買取 #古物商 #中小企業DX"
    prefix = "📝 新記事を公開しました\n\n"
    reserved = len(prefix) + len(hashtags) + len(result.note_url) + 4
    body_room = max_len - reserved
    if len(title) > body_room:
        title = title[: body_room - 1] + "…"
    return f"{prefix}{title}\n\n{result.note_url}{hashtags}"


def share_to_x(result: PostResult, config: XConfig | None = None) -> dict:
    """Post share tweet. Returns status dict.

    Implementation pending: requires tweepy or httpx + OAuth1 signing.
    See docs/x_setup.md for manual setup and the list of dependencies to add.
    """
    config = config or XConfig.from_env()
    if not config.is_ready():
        return {
            "status": "skipped",
            "reason": "X config not ready (disabled or missing credentials)",
        }
    raise NotImplementedError(
        "X API呼び出しは未実装。tweepy導入＋OAuth1署名実装が必要。"
        "docs/x_setup.md の手動セットアップ完了後に実装を有効化してください。"
    )
