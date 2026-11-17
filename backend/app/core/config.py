import os
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

# Query parameters libpq understands but asyncpg does not.
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding", "sslrootcert", "sslcert", "sslkey", "options"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database — any Postgres URL works: the plain postgresql://… string Neon/Vercel
    # gives you, or the older postgresql+asyncpg://… form.
    DATABASE_URL: str = "postgresql+asyncpg://snipster:snipster@localhost:5432/snipster"

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"

    # Storage (Cloudflare R2 / Backblaze B2 — S3-compatible)
    STORAGE_ENDPOINT: str = ""
    STORAGE_BUCKET: str = "snipster"
    STORAGE_ACCESS_KEY: str = ""
    STORAGE_SECRET_KEY: str = ""
    STORAGE_PUBLIC_BASE_URL: str = ""

    # Gemini
    GEMINI_API_KEY: str = ""

    # Google OAuth (YouTube)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"

    # App
    SECRET_KEY: str = "change-me"
    FRONTEND_URL: str = "http://localhost:5173"
    TOKEN_ENCRYPTION_KEY: str = ""

    # Password for the whole API. Empty = no login, which is fine on localhost but is
    # refused on Vercel (see require_admin). When set, SECRET_KEY must be a real random value.
    ADMIN_PASSWORD: str = ""
    # Extra browser origins allowed to call the API (only needed if the UI is hosted elsewhere).
    CORS_ORIGINS: str = ""

    # Direct-to-storage uploads (the browser uploads to R2, not through the API)
    MAX_UPLOAD_MB: int = 4096

    # Clip constraints (from PRD)
    CLIP_MIN_DURATION: int = 5    # seconds
    CLIP_MAX_DURATION: int = 90   # seconds
    CLIP_TARGET_COUNT: int = 5    # aim for this many clips per video

    # Whisper — "auto" picks CUDA + float16 when a GPU is visible, else CPU + int8
    # Multilingual model: base.en is English-only and cannot transcribe Hindi.
    WHISPER_MODEL: str = "medium"
    # "" = auto-detect the spoken language; or force one, e.g. "hi" or "en".
    WHISPER_LANGUAGE: str = ""
    WHISPER_DEVICE: str = "auto"          # auto | cuda | cpu
    WHISPER_COMPUTE_TYPE: str = "auto"    # auto | float16 | int8_float16 | int8
    WHISPER_BEAM_SIZE: int = 1            # 1 = greedy decoding (fastest)
    WHISPER_VAD: bool = True              # skip silence instead of transcribing it

    # Font for burned-in subtitles when the video is Hindi (or another Devanagari language).
    # Must be installed on the worker PC. Nirmala UI ships with Windows; on Linux use "Noto Sans Devanagari".
    SUBTITLE_FONT_DEVANAGARI: str = "Nirmala UI"

    # ffmpeg — "auto" uses h264_nvenc when the build supports it, else libx264
    FFMPEG_ENCODER: str = "auto"          # auto | h264_nvenc | libx264

    # Local cache for source videos, so each clip render doesn't re-download
    # the whole file from R2. "auto" = system temp dir; empty = caching off.
    MEDIA_CACHE_DIR: str = "auto"
    MEDIA_CACHE_MAX_AGE_HOURS: int = 6

    # Cap the resolution yt-dlp fetches — output is 1080x1920, so anything
    # larger is wasted bandwidth and decode time. 0 disables the cap.
    MAX_SOURCE_HEIGHT: int = 1080

    # ── Autopilot (worker side) ──────────────────────────────────────────────
    # YouTube Data API key [API + worker]. The worker uses it to list the channel's uploads;
    # the API uses it for the Dashboard's view/like/comment numbers.
    YOUTUBE_API_KEY: str = ""
    # The channel autopilot picks videos from (starts with UC). Used when the YouTube sign-in did not
    # save one (the upload permission cannot read channel info). Find it with the handle resolver in
    # DEPLOY.md, or in YouTube Studio > Settings > Channel > Advanced settings.
    YOUTUBE_CHANNEL_ID: str = ""
    # How often the scheduler wakes the autopilot tick, in minutes.
    AUTOPILOT_TICK_MINUTES: int = 5
    # Only pick videos within these lengths (seconds). The minimum also keeps Shorts out.
    AUTOPILOT_MIN_DURATION: int = 180
    AUTOPILOT_MAX_DURATION: int = 3600
    # Pages of 50 uploads to read when looking for an unused video.
    AUTOPILOT_MAX_PAGES: int = 10

    # Alerts (worker side). Create a bot with @BotFather; the chat id is the number from
    # https://api.telegram.org/bot<token>/getUpdates after you message the bot once.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    # Dead-man's switch: the worker requests this URL after every healthy tick (healthchecks.io etc.).
    HEALTHCHECK_URL: str = ""

    # ── Derived database settings ────────────────────────────────────────────
    @property
    def is_vercel(self) -> bool:
        """True when running as a Vercel Function (Vercel sets VERCEL=1)."""
        return bool(os.environ.get("VERCEL"))

    def _db_parts(self):
        url = self.DATABASE_URL.strip()
        for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgres://"):
            if url.startswith(prefix):
                url = "postgresql://" + url[len(prefix):]
                break
        parts = urlsplit(url)
        if parts.scheme != "postgresql":
            raise ValueError("DATABASE_URL must be a postgresql:// (or postgres://) URL")
        return parts, dict(parse_qsl(parts.query, keep_blank_values=True))

    @property
    def async_database_url(self) -> str:
        """URL for SQLAlchemy + asyncpg. libpq-only parameters (sslmode…) are removed."""
        parts, query = self._db_parts()
        query = {k: v for k, v in query.items() if k not in _LIBPQ_ONLY_PARAMS}
        return urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, urlencode(query), ""))

    @property
    def sync_database_url(self) -> str:
        """URL for SQLAlchemy + psycopg2 (used by the Celery worker)."""
        parts, query = self._db_parts()
        wants_ssl = self._wants_ssl(parts, query)  # decide BEFORE dropping asyncpg's "ssl" key
        query = {k: v for k, v in query.items() if k != "ssl"}
        if wants_ssl:
            query.setdefault("sslmode", "require")
        return urlunsplit(("postgresql+psycopg2", parts.netloc, parts.path, urlencode(query), ""))

    @staticmethod
    def _wants_ssl(parts, query) -> bool:
        mode = (query.get("sslmode") or query.get("ssl") or "").lower()
        return mode in ("require", "verify-ca", "verify-full", "true", "1")

    @property
    def database_connect_args(self) -> dict:
        """Extra asyncpg arguments: TLS when the URL asks for it, and PgBouncer-safe statements."""
        parts, query = self._db_parts()
        args: dict = {}
        if self._wants_ssl(parts, query):
            args["ssl"] = "require"
        host = parts.hostname or ""
        # Neon's pooled endpoint ("-pooler") sits behind PgBouncer in transaction mode,
        # which cannot reuse asyncpg's named prepared statements.
        if "-pooler" in host or os.environ.get("DATABASE_POOLED") == "1":
            import uuid

            args.update(
                statement_cache_size=0,
                prepared_statement_cache_size=0,
                prepared_statement_name_func=lambda: f"__asyncpg_{uuid.uuid4()}__",
            )
        return args


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
