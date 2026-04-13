"""タイトル最適化モジュール

5型(ベネフィット/数字/質問/How-to/比較)の候補をGemini 2.0 Flashで生成し、
スコアリング(0-100)してベスト候補を選ぶ。
"""
import json
import re

from google import genai
from google.genai import types

MODEL = "gemini-2.0-flash"

TITLE_TYPES = [
    {
        "id": "benefit",
        "name": "ベネフィット型",
        "guide": "読者が得られる具体的な価値・成果を明示する(例: 月収+20万円、作業時間半減)",
    },
    {
        "id": "number",
        "name": "数字型",
        "guide": "具体的な数字を含める(例: 7つの方法、3ステップ、年商1000万)",
    },
    {
        "id": "question",
        "name": "質問型",
        "guide": "読者の悩みを疑問文にする(例: 〜していませんか?、なぜ〜なのか?)",
    },
    {
        "id": "howto",
        "name": "How-to型",
        "guide": "「〜する方法」「〜のやり方」で解決手段を示す",
    },
    {
        "id": "comparison",
        "name": "比較型",
        "guide": "複数の選択肢・ビフォーアフターを対比する(例: AとBどっちが得?、従来法vs新手法)",
    },
]


def _extract_json(text: str):
    """レスポンスからJSON配列/オブジェクトを抽出する。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def generate_title_candidates(
    theme: str,
    main_keyword: str,
    sub_keywords: str,
    body_summary: str,
    client: genai.Client | None = None,
) -> list[dict]:
    """5型のタイトル候補を生成する。

    Returns:
        [{"type": "benefit", "title": "..."}, ...] の5件
    """
    client = client or genai.Client()

    types_desc = "\n".join(
        f"- {t['id']} ({t['name']}): {t['guide']}" for t in TITLE_TYPES
    )

    system_prompt = (
        "あなたはnoteのタイトル最適化の専門家です。"
        "出張買取業界の読者(同業者・開業希望者)にクリックされるタイトルを5つの型で作成します。"
        "各タイトルは32文字以内、数字や具体性を重視し、煽りすぎず実務者に刺さる表現にしてください。"
    )

    user_prompt = f"""以下の記事について、5つの型でタイトル候補を1つずつ作成してください。

【テーマ】{theme}
【メインキーワード】{main_keyword}
【関連キーワード】{sub_keywords}
【本文サマリ】{body_summary}

【5つの型】
{types_desc}

必ず以下のJSON配列のみを出力してください(前置き・説明文なし):
[
  {{"type": "benefit", "title": "..."}},
  {{"type": "number", "title": "..."}},
  {{"type": "question", "title": "..."}},
  {{"type": "howto", "title": "..."}},
  {{"type": "comparison", "title": "..."}}
]"""

    response = client.models.generate_content(
        model=MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=1024,
        ),
    )
    raw = response.text
    candidates = _extract_json(raw)
    return [
        {"type": c.get("type", ""), "title": c.get("title", "").strip()}
        for c in candidates
        if c.get("title")
    ]


def score_titles(
    candidates: list[dict],
    theme: str,
    body_summary: str,
    client: genai.Client | None = None,
) -> list[dict]:
    """候補に0-100のスコアを付与する。

    評価軸: クリック率想定 / 検索流入 / 具体性 / 誇大表現でないか
    """
    if not candidates:
        return []

    client = client or genai.Client()

    titles_block = "\n".join(
        f"{i + 1}. [{c['type']}] {c['title']}" for i, c in enumerate(candidates)
    )

    system_prompt = (
        "あなたはnoteのタイトル評価の専門家です。"
        "各タイトルを0-100でスコアリングします。評価軸は"
        "(1)クリック率想定 (2)検索流入期待 (3)具体性・数字 (4)誇大でなく実務者に信頼されるか。"
    )

    user_prompt = f"""以下の記事に対する候補タイトルをスコアリングしてください。

【テーマ】{theme}
【本文サマリ】{body_summary}

【候補】
{titles_block}

必ず以下のJSON配列のみを出力してください(前置き・説明文なし):
[
  {{"index": 1, "score": 数値, "reason": "短評"}},
  ...
]"""

    response = client.models.generate_content(
        model=MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=1024,
        ),
    )
    raw = response.text
    scores = _extract_json(raw)

    scored = []
    for i, c in enumerate(candidates):
        entry = dict(c)
        entry["score"] = 0
        entry["reason"] = ""
        for s in scores:
            if s.get("index") == i + 1:
                try:
                    entry["score"] = float(s.get("score", 0))
                except (TypeError, ValueError):
                    entry["score"] = 0
                entry["reason"] = s.get("reason", "")
                break
        scored.append(entry)
    return scored


def pick_best(candidates: list[dict]) -> str:
    """スコア最高の候補のタイトルを返す。"""
    if not candidates:
        return ""
    best = max(candidates, key=lambda c: c.get("score", 0))
    return best.get("title", "")


def optimize_title(
    theme: str,
    main_keyword: str,
    sub_keywords: str,
    body_summary: str,
    client: genai.Client | None = None,
) -> tuple[str, list[dict]]:
    """5型生成→スコアリング→ベスト選定のフルフロー。

    Returns:
        (best_title, scored_candidates)
    """
    client = client or genai.Client()
    candidates = generate_title_candidates(
        theme, main_keyword, sub_keywords, body_summary, client=client
    )
    scored = score_titles(candidates, theme, body_summary, client=client)
    return pick_best(scored), scored
