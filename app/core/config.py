from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_env_files() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (ROOT / ".env",):
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
        self.log_file = env("LOG_FILE", "logs/recall_tools.log")
        self.recall_data_db_path = env("RECALL_DATA_DB_PATH", "data/recall_tools.sqlite3")
        self.bot_name = env("BOT_NAME", "Mirako Recall Bridge")
        self.recall_bot_variant = env("RECALL_BOT_VARIANT", "web_4_core")
        self.recall_webhook_secret = env("RECALL_WEBHOOK_SECRET")
        self.recall_transcript_enabled = env("RECALL_TRANSCRIPT_ENABLED", "true").lower() in {"1", "true", "yes"}
        self.recall_transcript_language_code = env("RECALL_TRANSCRIPT_LANGUAGE_CODE", "en")
        self.recall_transcript_mode = env("RECALL_TRANSCRIPT_MODE", "prioritize_low_latency")
        self.recall_everyone_left_timeout_seconds = env_int("RECALL_EVERYONE_LEFT_TIMEOUT_SECONDS", 300)
        self.recall_everyone_left_activate_after_seconds = env_int("RECALL_EVERYONE_LEFT_ACTIVATE_AFTER_SECONDS", 1)
        self.bot_only_cleanup_enabled = env("BOT_ONLY_CLEANUP_ENABLED", "true").lower() in {"1", "true", "yes"}
        self.bot_only_cleanup_seconds = env_int("BOT_ONLY_CLEANUP_SECONDS", 300)
        self.bot_only_cleanup_interval_seconds = env_int("BOT_ONLY_CLEANUP_INTERVAL_SECONDS", 30)

        self.conversation_mode_policy = env("CONVERSATION_MODE_POLICY", "auto").strip().lower()
        if self.conversation_mode_policy not in {"auto", "multi", "single"}:
            self.conversation_mode_policy = "auto"

        self.recall_video_separate_h264_enabled = env(
            "RECALL_VIDEO_SEPARATE_H264_ENABLED", "true"
        ).lower() in {"1", "true", "yes"}
        self.recall_video_mixed_layout = env("RECALL_VIDEO_MIXED_LAYOUT", "gallery_view_v2")
        self.recall_realtime_ws_url = env(
            "RECALL_REALTIME_WS_URL", "wss://us-east-1.recall.ai/realtime"
        )
        self.frame_cache_max_participants = env_int("FRAME_CACHE_MAX_PARTICIPANTS", 32)
        self.frame_cache_max_age_seconds = env_int("FRAME_CACHE_MAX_AGE_SECONDS", 300)
        self.frame_cache_max_bytes = env_int("FRAME_CACHE_MAX_BYTES", 64 * 1024 * 1024)

        self.minimax_api_base = env("MINIMAX_API_BASE", "https://api.minimaxi.com/v1")
        self.minimax_api_key = env("MINIMAX_API_KEY")
        self.minimax_model = env("MINIMAX_MODEL", "MiniMax-VL-01")
        self.minimax_timeout_seconds = env_int("MINIMAX_TIMEOUT_SECONDS", 60)
        self.capture_prompt = env(
            "CAPTURE_PROMPT",
            "Please describe what is on this meeting screen. Focus on text content, slide titles, bullet points, and key data. If this is a shared screen or slide, transcribe the visible text. Respond concisely.",
        )

        self.zoom_oauth_client_id = env("ZOOM_OAUTH_CLIENT_ID")
        self.zoom_oauth_client_secret = env("ZOOM_OAUTH_CLIENT_SECRET")
        self.zoom_oauth_account_id = env("ZOOM_OAUTH_ACCOUNT_ID")
        self.zoom_create_user_id = env("ZOOM_CREATE_USER_ID")
        self.zoom_create_topic = env("ZOOM_CREATE_TOPIC", "Mirako Recall Bridge")
        self.zoom_create_duration_minutes = env_int("ZOOM_CREATE_DURATION_MINUTES", 60)
        self.zoom_signed_in_enabled = env("ZOOM_SIGNED_IN_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        self.zoom_zak_user_id = env("ZOOM_ZAK_USER_ID", self.zoom_create_user_id or "me")
        self.zoom_zak_cache_ttl_seconds = env_int("ZOOM_ZAK_CACHE_TTL_SECONDS", 5400)
        self.zoom_zak_callback_secret = env("ZOOM_ZAK_CALLBACK_SECRET", self.service_api_key)


settings = Settings()
