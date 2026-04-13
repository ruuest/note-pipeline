"""
note サムネイル自動生成（Playwright HTML→PNG）

カテゴリ別カラーテーマで記事タイトルをオーバーレイした
1280x670 PNG を生成する。外部API不要。

使い方:
    from src.thumbnail import generate_thumbnail
    path = generate_thumbnail(title="出張買取の始め方", category="開業")
"""
from __future__ import annotations

import logging
import textwrap
from datetime import datetime
from html import escape
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
THUMBNAILS_DIR = BASE_DIR / "drafts" / "thumbnails"

# カテゴリ別グラデーション + アクセント色
CATEGORY_THEMES: dict[str, dict] = {
    "開業": {
        "gradient": "linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%)",
        "accent": "#e94560",
        "text_color": "#ffffff",
    },
    "営業": {
        "gradient": "linear-gradient(135deg, #0d1b2a 0%, #1b263b 50%, #415a77 100%)",
        "accent": "#e0a458",
        "text_color": "#ffffff",
    },
    "経営": {
        "gradient": "linear-gradient(135deg, #2d2d2d 0%, #3d3d3d 50%, #4a4a4a 100%)",
        "accent": "#00b4d8",
        "text_color": "#ffffff",
    },
    "買取": {
        "gradient": "linear-gradient(135deg, #1b2838 0%, #2a4056 50%, #3a5a7c 100%)",
        "accent": "#f4a261",
        "text_color": "#ffffff",
    },
    "相場": {
        "gradient": "linear-gradient(135deg, #0b1929 0%, #132f4c 50%, #1a4672 100%)",
        "accent": "#4fc3f7",
        "text_color": "#ffffff",
    },
    "集客": {
        "gradient": "linear-gradient(135deg, #1a0a2e 0%, #2d1b4e 50%, #4a2c6e 100%)",
        "accent": "#ff6b9d",
        "text_color": "#ffffff",
    },
    "マーケティング": {
        "gradient": "linear-gradient(135deg, #1a0a2e 0%, #2d1b4e 50%, #4a2c6e 100%)",
        "accent": "#ff6b9d",
        "text_color": "#ffffff",
    },
}

DEFAULT_THEME = {
    "gradient": "linear-gradient(135deg, #1a1a2e 0%, #232946 50%, #3a3f7a 100%)",
    "accent": "#eebbc3",
    "text_color": "#ffffff",
}

# note.com 推奨サイズ
WIDTH = 1280
HEIGHT = 670


def _get_theme(category: str) -> dict:
    """カテゴリに部分一致するテーマを返す。"""
    for key, theme in CATEGORY_THEMES.items():
        if key in category:
            return theme
    return DEFAULT_THEME


def _build_html(title: str, category: str) -> str:
    """サムネイル用HTMLを生成。"""
    theme = _get_theme(category)
    safe_title = escape(title)

    # タイトルの長さに応じてフォントサイズ調整
    if len(title) <= 15:
        font_size = 52
    elif len(title) <= 25:
        font_size = 44
    elif len(title) <= 35:
        font_size = 38
    else:
        font_size = 32

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    width: {WIDTH}px;
    height: {HEIGHT}px;
    background: {theme["gradient"]};
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: "Hiragino Kaku Gothic ProN", "Noto Sans JP", "Yu Gothic", sans-serif;
    overflow: hidden;
    position: relative;
  }}
  /* 装飾用の幾何学パターン */
  .deco-circle {{
    position: absolute;
    border-radius: 50%;
    opacity: 0.06;
    background: {theme["accent"]};
  }}
  .deco-circle.c1 {{ width: 400px; height: 400px; top: -120px; right: -80px; }}
  .deco-circle.c2 {{ width: 250px; height: 250px; bottom: -60px; left: -40px; }}
  .deco-circle.c3 {{ width: 180px; height: 180px; top: 50%; left: 65%; opacity: 0.04; }}
  /* アクセントライン */
  .accent-bar {{
    position: absolute;
    left: 80px;
    top: 50%;
    transform: translateY(-50%);
    width: 5px;
    height: 160px;
    background: {theme["accent"]};
    border-radius: 3px;
  }}
  .content {{
    max-width: 1000px;
    padding: 60px 100px 60px 120px;
    z-index: 1;
  }}
  .category {{
    display: inline-block;
    font-size: 16px;
    font-weight: 600;
    color: {theme["accent"]};
    letter-spacing: 0.15em;
    text-transform: uppercase;
    margin-bottom: 20px;
    padding: 4px 0;
  }}
  .title {{
    font-size: {font_size}px;
    font-weight: 900;
    color: {theme["text_color"]};
    line-height: 1.45;
    letter-spacing: 0.02em;
    text-shadow: 0 2px 12px rgba(0,0,0,0.3);
  }}
  .brand {{
    position: absolute;
    bottom: 32px;
    right: 48px;
    font-size: 14px;
    color: rgba(255,255,255,0.35);
    letter-spacing: 0.1em;
  }}
</style>
</head>
<body>
  <div class="deco-circle c1"></div>
  <div class="deco-circle c2"></div>
  <div class="deco-circle c3"></div>
  <div class="accent-bar"></div>
  <div class="content">
    <div class="category">{escape(category)}</div>
    <div class="title">{safe_title}</div>
  </div>
  <div class="brand">NV CLOUD</div>
</body>
</html>"""


def _output_path(draft_id: str | None) -> Path:
    THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    if draft_id is None:
        draft_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    return THUMBNAILS_DIR / f"{draft_id}.png"


def generate_thumbnail(
    title: str,
    category: str,
    draft_id: str | None = None,
) -> Path:
    """Playwright で HTML→PNG サムネイルを生成。

    Args:
        title: 記事タイトル
        category: カテゴリ（カラーテーマ選択に使用）
        draft_id: 出力ファイル名のベース

    Returns:
        生成された PNG のパス

    Raises:
        RuntimeError: Playwright が使えない場合
    """
    from playwright.sync_api import sync_playwright

    out_path = _output_path(draft_id)
    html = _build_html(title, category)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        page.set_content(html, wait_until="networkidle")
        page.screenshot(path=str(out_path), type="png")
        browser.close()

    logger.info(f"サムネイル生成: {out_path}")
    return out_path
