"""マガジン振り分け / コンテスト応募のスタブ。
note の公開APIに該当エンドポイントがなく、内部APIの仕様調査中のため未実装。
リサーチ大臣による仕様確定後に実装する。
"""
from src.models import Article, PostResult


MAGAZINE_BY_CATEGORY: dict[str, list[str]] = {
    "開業": ["出張買取の始め方", "古物商の実務"],
    "経営": ["買取業経営ノウハウ"],
    "業務効率化": ["買取DX事例集"],
    "法令遵守": ["古物商コンプライアンス"],
    "スキルアップ": ["査定・目利き術"],
}


def pick_magazines(article: Article) -> list[str]:
    return MAGAZINE_BY_CATEGORY.get(article.category, [])


def add_to_magazines(result: PostResult, magazines: list[str]) -> dict:
    """TODO(minister_research): note マガジン追加のAPI仕様確定後に実装。
    現状は Playwright でエディタに遷移してGUIから追加する案が有力だが、
    publisher.py の投稿フローとは別セッションで実行する必要がある。"""
    return {
        "status": "not_implemented",
        "reason": "note magazine API spec unknown (research pending)",
        "planned": magazines,
        "note_url": result.note_url,
    }


CONTEST_KEYWORDS: dict[str, list[str]] = {
    "#開業コンテスト": ["開業"],
    "#DX事例": ["業務効率化"],
    "#中小企業診断": ["経営"],
}


def find_matching_contests(article: Article) -> list[str]:
    matches = []
    for contest, categories in CONTEST_KEYWORDS.items():
        if article.category in categories:
            matches.append(contest)
    return matches


def submit_to_contests(result: PostResult, contests: list[str]) -> dict:
    """TODO(minister_research): note コンテスト応募フロー調査後に実装。"""
    return {
        "status": "not_implemented",
        "reason": "note contest submission flow unknown (research pending)",
        "planned": contests,
        "note_url": result.note_url,
    }
