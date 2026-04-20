"""note 自動ログインの単体デバッグ用スクリプト。

使い方:
    cd /Users/apple/NorthValueAsset/note-pipeline
    python3 -m src.debug_login

動作:
    - .env を読み込み NOTE_EMAIL / NOTE_PASSWORD を取得
    - NotePublisher.start() で Chromium (headless=False) を起動
    - _auto_login() を直接実行し、成功すれば .note-session.json を保存
    - 失敗時は Telegram 通知も飛ばないよう dry モードで _notify_session_issue を抑止

セッション切れの 20:00 cron 失敗再現 + selector 検証に使う。
"""

import asyncio
import os
import sys
from pathlib import Path

# .env を自前で読み込む（python-dotenv が未インストールでも動くように）
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


def _load_env() -> None:
    if not ENV_FILE.exists():
        print(f"⚠ .env が見つからない: {ENV_FILE}")
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def _run() -> int:
    _load_env()

    from src.publisher import NotePublisher, NoteSessionError

    email = os.environ.get("NOTE_EMAIL", "").strip()
    password = os.environ.get("NOTE_PASSWORD", "").strip()
    if not email or not password:
        print("❌ NOTE_EMAIL / NOTE_PASSWORD が .env にない")
        return 1
    print(f"🔐 debug_login 開始 (email={email[:3]}***)")

    publisher = NotePublisher()
    # _notify_session_issue を抑止（デバッグ時に Telegram 汚染しないため）
    publisher._notify_session_issue = lambda *a, **kw: None
    await publisher.start()
    try:
        context = await publisher._auto_login()
        if context is None:
            print("❌ _auto_login returned None（env 未設定？）")
            return 1
        print("✅ _auto_login 成功、storage_state 保存済")
        await context.close()
        return 0
    except NoteSessionError as e:
        print(f"❌ NoteSessionError: {e}")
        print("   → logs/screenshots/auto_login_* を確認してください")
        return 2
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
        return 3
    finally:
        await publisher.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
