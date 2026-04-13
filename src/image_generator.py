"""
note アイキャッチ画像自動生成（骨組み）

プロバイダ:
  - 'dalle3'        : OpenAI DALL-E 3 API
  - 'stability'     : Stability AI SD3 API
  - 'comfyui_local' : ローカル ComfyUI サーバー経由
  - 'none'          : 生成しない（config デフォルト）

使い方:
    from src.image_generator import generate_eyecatch
    path = generate_eyecatch(
        title="出張買取の始め方",
        theme="開業ノウハウ",
        provider="dalle3",
        draft_id="20260410_120000_kaitori",
    )

注意:
  この実装は骨組み（スタブ）です。OpenAI/Stability の API キーが
  .env に設定されていない状態では使用できません。
  利用前に config/templates.json の image_generation.enabled を true に、
  provider を明示的に指定してください。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

BASE_DIR = Path(__file__).resolve().parent.parent
DRAFTS_IMAGES_DIR = BASE_DIR / "drafts" / "images"

Provider = Literal["dalle3", "stability", "comfyui_local", "none"]


def _build_prompt(title: str, theme: str) -> str:
    """日本のビジネス系ブログ記事アイキャッチ向けプロンプトを構築。

    note のサムネイルは 1280x670 相当で表示されるので、横長・中央に余白が
    取れる構図を指示する。文字は入れず、抽象ビジュアルにする。
    """
    return (
        f"A clean, modern, flat-style illustration for a Japanese business blog "
        f"article about \"{title}\" (theme: {theme}). Abstract visual metaphor, "
        f"no text, no letters, no Japanese characters. Calm color palette with "
        f"one accent color. Horizontal composition, center-weighted, generous "
        f"negative space. Professional, trustworthy, not cartoonish."
    )


def _ensure_output_dir() -> Path:
    DRAFTS_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return DRAFTS_IMAGES_DIR


def _output_path(draft_id: str) -> Path:
    return _ensure_output_dir() / f"{draft_id}.png"


def _generate_dalle3(prompt: str, out_path: Path) -> Path:
    """OpenAI DALL-E 3 で生成（スタブ）。

    実装時の想定:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1792x1024",   # 横長
            quality="standard", # "hd" にすると $0.08 → $0.12
            n=1,
        )
        url = resp.data[0].url
        # requests で DL して out_path に保存
    コスト目安: 1枚 $0.04 (standard 1792x1024) / $0.12 (hd)
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY が設定されていません。.env に追加してください。"
        )
    raise NotImplementedError(
        "DALL-E 3 連携は未実装。projects/note_growth_strategy/"
        "07_eyecatch_provider_comparison.md を参照して実装してください。"
    )


def _generate_stability(prompt: str, out_path: Path) -> Path:
    """Stability AI (SD3) で生成（スタブ）。

    実装時の想定:
        import requests
        r = requests.post(
            "https://api.stability.ai/v2beta/stable-image/generate/sd3",
            headers={
                "Authorization": f"Bearer {os.environ['STABILITY_API_KEY']}",
                "Accept": "image/*",
            },
            files={"none": ""},
            data={
                "prompt": prompt,
                "aspect_ratio": "16:9",
                "model": "sd3-medium",
                "output_format": "png",
            },
        )
        out_path.write_bytes(r.content)
    コスト目安: SD3 Medium 3.5 credits/枚 ≈ $0.035
    """
    if not os.environ.get("STABILITY_API_KEY"):
        raise RuntimeError(
            "STABILITY_API_KEY が設定されていません。.env に追加してください。"
        )
    raise NotImplementedError(
        "Stability AI 連携は未実装。projects/note_growth_strategy/"
        "07_eyecatch_provider_comparison.md を参照して実装してください。"
    )


def _generate_comfyui_local(prompt: str, out_path: Path) -> Path:
    """ローカル ComfyUI サーバー経由で生成（スタブ）。

    セットアップ手順（実装・実行は行わない。天皇環境依存）:
      1. ComfyUI をクローン:
         git clone https://github.com/comfyanonymous/ComfyUI ~/ComfyUI
      2. 依存インストール:
         cd ~/ComfyUI && pip install -r requirements.txt
      3. モデルを配置:
         models/checkpoints/ に SDXL or SD3 の .safetensors を置く
         推奨: sd_xl_base_1.0.safetensors (約6.9GB)
      4. 起動:
         python main.py --listen 127.0.0.1 --port 8188
      5. API 呼び出し:
         POST http://127.0.0.1:8188/prompt
         body: ComfyUI workflow JSON (ノードグラフ)

    実装時の想定:
        import requests, uuid, time
        workflow = _build_comfyui_workflow(prompt)
        client_id = str(uuid.uuid4())
        r = requests.post(
            "http://127.0.0.1:8188/prompt",
            json={"prompt": workflow, "client_id": client_id},
        )
        prompt_id = r.json()["prompt_id"]
        # /history/{prompt_id} を poll して完了を待つ
        # /view?filename=... で画像取得 → out_path に保存

    GPU要件: NVIDIA GPU VRAM 8GB以上推奨（SDXL）、12GB以上快適（SD3）
    Mac M1/M2/M3: MPS バックエンドで動作、M2 Pro 16GB で約30秒/枚
    """
    raise NotImplementedError(
        "ComfyUI ローカル連携は未実装。上記コメントのセットアップ手順を"
        "完了後に実装してください。"
    )


def generate_eyecatch(
    title: str,
    theme: str,
    provider: Provider = "dalle3",
    draft_id: str | None = None,
) -> Path:
    """記事アイキャッチ画像を生成し、保存パスを返す。

    Args:
        title: 記事タイトル
        theme: 記事テーマ（プロンプト用語）
        provider: 画像生成プロバイダ
        draft_id: 保存ファイル名の base（未指定ならタイムスタンプ）

    Returns:
        生成画像の Path

    Raises:
        RuntimeError: API キー未設定
        NotImplementedError: 骨組み段階のためスタブ
        ValueError: 不正なプロバイダ
    """
    if provider == "none":
        raise ValueError(
            "provider='none' では画像生成は行いません。呼び出し側で分岐してください。"
        )

    if draft_id is None:
        from datetime import datetime
        draft_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    out_path = _output_path(draft_id)
    prompt = _build_prompt(title, theme)

    if provider == "dalle3":
        return _generate_dalle3(prompt, out_path)
    if provider == "stability":
        return _generate_stability(prompt, out_path)
    if provider == "comfyui_local":
        return _generate_comfyui_local(prompt, out_path)

    raise ValueError(f"未知のプロバイダ: {provider}")
