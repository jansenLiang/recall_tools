from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_env_files() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (ROOT.parent / "recall_demo" / ".env", ROOT / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            values[key.strip()] = raw_value.strip().strip("\"'")
    return values


_ENV_FILES = load_env_files()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, _ENV_FILES.get(name, default))


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)).strip())
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.recall_api_key = env("RECALL_API_KEY")
        self.recall_base_url = env("RECALL_BASE_URL", "https://us-east-1.recall.ai/api/v1")

        self.public_base_url = env("PUBLIC_BASE_URL", "http://localhost:8000")
        self.live_stream_gateway_url = env("LIVE_STREAM_GATEWAY_URL")
        self.service_api_key = env("SERVICE_API_KEY")

        self.app_host = env("APP_HOST", "0.0.0.0")
        self.app_port = env_int("APP_PORT", 8000)
        self.log_level = env("LOG_LEVEL", "INFO")
        self.bot_name = env("BOT_NAME", "Mirako Recall Bridge")
        self.recall_bot_variant = env("RECALL_BOT_VARIANT", "web_4_core")

        self.zoom_oauth_client_id = env("ZOOM_OAUTH_CLIENT_ID")
        self.zoom_oauth_client_secret = env("ZOOM_OAUTH_CLIENT_SECRET")
        self.zoom_oauth_account_id = env("ZOOM_OAUTH_ACCOUNT_ID")
        self.zoom_create_user_id = env("ZOOM_CREATE_USER_ID")
        self.zoom_create_topic = env("ZOOM_CREATE_TOPIC", "Mirako Recall Bridge")
        self.zoom_create_duration_minutes = env_int("ZOOM_CREATE_DURATION_MINUTES", 60)


settings = Settings()
