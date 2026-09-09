"""Configuration: on-disk JSON, held in memory as the source of truth.

DESIGN.md requires the in-memory object to be authoritative and written
through to disk on save, rather than re-read each poll cycle - re-reading
races with the Settings page mid-write and can load a half-written file.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

CREDENTIALS_PATH = DATA_DIR / "credentials.json"
TOKEN_PATH = DATA_DIR / "token.json"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "classifications.jsonl"


class Config(BaseModel):
    poll_interval_seconds: int = Field(default=60, ge=10)
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    # Independent of the argmax: if p(To-Action) clears this, INBOX is kept.
    # Protects the only error class that costs anything - a missed bill.
    to_action_floor: float = Field(default=0.15, ge=0.0, le=1.0)
    ollama_model: str = "llama3.1:8b"
    # Defaults to True so a fresh install cannot write to the inbox by
    # accident. Turning it off is a deliberate act.
    dry_run: bool = True
    max_failures_before_error: int = Field(default=3, ge=1)


_config: Config | None = None


def load() -> Config:
    """Load from disk into memory. Missing file means defaults."""
    global _config
    if CONFIG_PATH.exists():
        _config = Config.model_validate_json(CONFIG_PATH.read_text())
    else:
        _config = Config()
    return _config


def get() -> Config:
    """The in-memory config. Loads once on first access."""
    return _config if _config is not None else load()


def save(config: Config) -> Config:
    """Write through to disk and make it the in-memory truth."""
    global _config
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(config.model_dump_json(indent=2) + "\n")
    _config = config
    return _config
