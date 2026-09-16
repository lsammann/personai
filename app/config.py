"""Configuration: on-disk JSON, held in memory as the source of truth.

DESIGN.md requires the in-memory object to be authoritative and written
through to disk on save, rather than re-read each poll cycle - re-reading
races with the Settings page mid-write and can load a half-written file.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

CREDENTIALS_PATH = DATA_DIR / "credentials.json"
TOKEN_PATH = DATA_DIR / "token.json"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "classifications.jsonl"

# Committed, not in data/: the promotional-domain allowlist is tuning data
# rather than a secret, so versioning it makes every change a reviewable diff
# and a fresh clone works with no setup step.
PREFILTER_PATH = REPO_ROOT / "prefilter_domains.json"


class Config(BaseModel):
    poll_interval_seconds: int = Field(default=60, ge=10)
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    # Independent of the argmax: if p(To Action) clears this, INBOX is kept.
    # Protects the only error class that costs anything - a missed bill.
    #
    # DORMANT on llama3.1:8b, measured in Phase 2 and replicated on the
    # holdout: the sweep returns identical results at 0.05, 0.10 and 0.15
    # because the first-token distribution is near one-hot (median confidence
    # 0.982), so a message whose argmax is Receipts essentially never carries
    # 15% residual mass on To Action. Kept because it costs nothing and a less
    # saturated model would make it live again - but the protection that
    # actually operates is Needs Review plus KEEPS_INBOX. See DESIGN.md.
    to_action_floor: float = Field(default=0.15, ge=0.0, le=1.0)
    ollama_model: str = "llama3.1:8b"
    # How much of the body is sent to the model. **Measured**, not a
    # placeholder: Phase 2 swept 0 / 300 / 800 / 1500 / 3000 and 300 won on
    # every axis. Against 0 it fixes 8 of the 20 To Action messages and breaks
    # none (p=0.008); above 300 accuracy declines monotonically, because the
    # boilerplate tail - unsubscribe footers, social links, legal text - drags
    # predictions toward Promotions (53 -> 66 predictions at 3000, truth 58).
    # It is also the dominant latency lever, the call emitting one token: 2.6s
    # here against 7.9s at 1500. 0 is allowed: subject-and-sender-only is a
    # legitimate experiment, not a broken config.
    body_chars: int = Field(default=300, ge=0)
    # Defaults to True so a fresh install cannot write to the inbox by
    # accident. Turning it off is a deliberate act.
    dry_run: bool = True
    max_failures_before_error: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _asymmetric_rule_must_be_reachable(self) -> Config:
        """The two thresholds are coupled, and nothing else says so.

        The asymmetric rule fires only when one category clears
        `confidence_threshold` AND `To Action` clears `to_action_floor` at the
        same time. Those two probabilities are part of the same distribution
        summing to 1, so if the thresholds sum to 1 or more no distribution can
        satisfy both and the rule is unreachable.

        That matters because the rule is the only thing protecting the only
        error class that costs anything - a missed bill - and it would fail
        silently: mail would keep flowing, `Needs Review` would keep working,
        and nothing would look wrong. Phase 2 tunes both values off the
        calibration table, which is exactly when this gets broken by accident.
        """
        total = self.confidence_threshold + self.to_action_floor
        if total >= 1.0:
            raise ValueError(
                f"confidence_threshold ({self.confidence_threshold}) + "
                f"to_action_floor ({self.to_action_floor}) = {total:.2f}, but a "
                "distribution sums to 1 - so no message could ever satisfy both "
                "and the asymmetric To Action rule would never fire. Lower one "
                "of them."
            )
        return self


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
