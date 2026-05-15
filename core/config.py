import os

class Settings:
    @property
    def SHARK_SIGNAL_SOURCE(self) -> str:
        return os.environ.get("SHARK_SIGNAL_SOURCE", "plan").strip().lower()

    @property
    def AI_ENABLED(self) -> bool:
        return os.environ.get("AI_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")

    @property
    def SHARK_REDIS_URL(self) -> str:
        return os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0").strip()

    @property
    def DATABASE_URL(self) -> str:
        return os.environ.get("DATABASE_URL", "postgresql://shark:shark@db:5432/shark").strip()

    @property
    def SHARK_MODE(self) -> str:
        return os.environ.get("SHARK_MODE", "paper").strip().lower()

    @property
    def SHARK_HTTP_PORT(self) -> int:
        return int(os.environ.get("SHARK_HTTP_PORT", "80"))

    @property
    def GATE_API_KEY(self) -> str:
        return os.environ.get("GATE_API_KEY", "").strip()

    @property
    def GATE_API_SECRET(self) -> str:
        return os.environ.get("GATE_API_SECRET", "").strip()

    @property
    def DEEPSEEK_API_KEY(self) -> str:
        return os.environ.get("DEEPSEEK_API_KEY", "").strip()

    @property
    def QWEN_KEY(self) -> str:
        return os.environ.get("QWEN_KEY", "").strip()

    @property
    def VOLC_KEY(self) -> str:
        return os.environ.get("VOLC_KEY", "").strip()

    @property
    def AI_COMMITTEE_FULL(self) -> bool:
        return os.environ.get("AI_COMMITTEE_FULL", "0").strip() == "1"

    @property
    def AI_COMMITTEE_VERBOSE(self) -> bool:
        return os.environ.get("AI_COMMITTEE_VERBOSE", "").strip().lower() in ("1", "true", "yes")

    @property
    def SLACK_WEBHOOK(self) -> str:
        return os.environ.get("SLACK_WEBHOOK", "").strip()

    @property
    def SLACK_ALERT_WEBHOOK(self) -> str:
        return os.environ.get("SLACK_ALERT_WEBHOOK", "").strip()

    @property
    def CHARACTER_LLM_MODEL(self) -> str:
        return os.environ.get("CHARACTER_LLM_MODEL", "deepseek-chat").strip()

    @property
    def CHARACTER_LLM_ENABLED(self) -> bool:
        return os.environ.get("CHARACTER_LLM", "1").strip() != "0"

    @property
    def DIALOGUE_AMMO_ENABLED(self) -> bool:
        return os.environ.get("DIALOGUE_AMMO", "1").strip() != "0"

    @property
    def SHARK_LICENSE_ENABLED(self) -> bool:
        return os.environ.get("SHARK_LICENSE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")

    @property
    def SHARK_DEVICE_LOCK(self) -> bool:
        return os.environ.get("SHARK_DEVICE_LOCK", "").strip().lower() in ("1", "true", "yes", "on")

    @property
    def SHARK_LOG_LEVEL(self) -> str:
        return os.environ.get("SHARK_LOG_LEVEL", "INFO").strip().upper()

    @property
    def SHARK_AUTO_START_PAPER(self) -> bool:
        return os.environ.get("SHARK_AUTO_START_PAPER", "1").strip().lower() in ("1", "true", "yes", "on")

    @property
    def SHARK_TRADING_TRACK(self) -> str:
        return os.environ.get("SHARK_TRADING_TRACK", "dual").strip().lower()

settings = Settings()
