"""サムネ before/after 比較サンプル生成。

logs/samples/ に:
  - before_01_*.png (旧 52/44/38/32 テーブル)
  - after_01_*.png  (新 78/72/64/58/52/46/40 テーブル)
を 3 タイトル分生成。commit 後の視認性確認に使う。
"""
from pathlib import Path
import sys
from html import escape

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.thumbnail import _build_html, _get_theme, WIDTH, HEIGHT  # noqa

SAMPLES_DIR = BASE_DIR / "logs" / "samples"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

# 3 タイトル（今夜投稿した記事感の典型）
TITLES = [
    ("出張買取の始め方完全ガイド", "開業"),
    ("金相場の読み方と仕入れ戦略", "相場"),
    ("リピート率を上げる顧客フォロー術", "営業"),
]


def _legacy_font_size(n: int) -> int:
    if n <= 15:
        return 52
    if n <= 25:
        return 44
    if n <= 35:
        return 38
    return 32


def _legacy_html(title: str, category: str) -> str:
    """旧フォントテーブルで HTML を組む（_build_html のミラー + font_size だけ差し替え）"""
    theme = _get_theme(category)
    safe_title = escape(title)
    font_size = _legacy_font_size(len(title))
    # _build_html と同じテンプレートを font_size だけ差し替えて生成
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
  .deco-circle {{
    position: absolute;
    border-radius: 50%;
    opacity: 0.06;
    background: {theme["accent"]};
  }}
  .deco-circle.c1 {{ width: 400px; height: 400px; top: -120px; right: -80px; }}
  .deco-circle.c2 {{ width: 250px; height: 250px; bottom: -60px; left: -40px; }}
  .deco-circle.c3 {{ width: 180px; height: 180px; top: 50%; left: 65%; opacity: 0.04; }}
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


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for idx, (title, category) in enumerate(TITLES, 1):
            # before
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            page.set_content(_legacy_html(title, category), wait_until="networkidle")
            b_path = SAMPLES_DIR / f"before_{idx:02d}_{category}.png"
            page.screenshot(path=str(b_path), type="png")
            page.close()
            print(f"  before: {b_path} (font_size={_legacy_font_size(len(title))})")

            # after
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            page.set_content(_build_html(title, category), wait_until="networkidle")
            a_path = SAMPLES_DIR / f"after_{idx:02d}_{category}.png"
            page.screenshot(path=str(a_path), type="png")
            page.close()
            # new font size lookup
            from src.thumbnail import _build_html as _bh  # noqa
            n = len(title)
            new_size = (78 if n <= 10 else 72 if n <= 15 else 64 if n <= 20
                        else 58 if n <= 25 else 52 if n <= 30 else 46 if n <= 35 else 40)
            print(f"  after:  {a_path} (font_size={new_size})")
        browser.close()


if __name__ == "__main__":
    main()
