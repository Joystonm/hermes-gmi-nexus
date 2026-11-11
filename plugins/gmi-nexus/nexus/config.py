"""Settings resolution for NEXUS.

Settings come from three places, in priority order:

1. ``plugins.entries.gmi-nexus.settings`` in ``config.yaml`` (via ``ctx.get_config``).
2. Environment variables, for the credentials Hermes does not store in config.
3. The defaults declared in ``plugin.yaml``'s ``config_schema``.

The credential itself is read through Hermes' secret scope
(``agent.secret_scope.get_secret``) so it honours the active profile and is
never written into ``config.yaml`` or into a log line. The process
environment is used for the credential only when Hermes is not present at
all — inside Hermes, the secret scope is the sole authority, so a key
belonging to another profile can never be picked up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError, NotConfiguredError

#: Name of the environment variable holding the GMI Cloud API key.
API_KEY_ENV = "GMI_API_KEY"

#: Optional environment variable overriding the inference base URL.
BASE_URL_ENV = "GMI_BASE_URL"


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass(slots=True)
class Settings:
    """The resolved configuration for one NEXUS session."""

    llm_base_url: str = "https://api.gmi-serving.com/v1"
    media_base_url: str = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey"
    mcp_server: str = "gmi"
    use_mcp: bool = False
    request_timeout_s: int = 120
    max_retries: int = 2
    max_parallel: int = 3
    max_cost_per_run_usd: float = 1.0
    daily_budget_usd: float = 5.0
    approval_threshold_usd: float = 0.25
    #: The token budget for a call that has to return whole files.
    #:
    #: The inference client's default of 2048 tokens is enough for a sentence
    #: and nowhere near enough for a source file. A reply that runs out of
    #: budget is cut off *mid-JSON*, and a half-written file list is not a
    #: parse error the model can be asked to fix — it is a request that was
    #: too small. The Dev Agent asks for a file at a time, so it asks for
    #: room to write one.
    code_max_tokens: int = 8192
    #: The token budget for a call that answers in prose.
    #:
    #: A reasoning model spends part of this budget thinking before it
    #: answers, and a live run showed what happens when there is not enough
    #: room for both: DeepSeek-V4-Flash used all 2048 of its completion
    #: tokens on reasoning, was cut off mid-thought and returned no text at
    #: all. A limit is a cap and not a charge — a model that answers in 200
    #: tokens is still billed for 200 — so the cap is set where a reasoning
    #: model has room for the reasoning *and* the answer.
    max_tokens: int = 4096
    preferred_models: list[str] = field(default_factory=list)
    blocked_models: list[str] = field(default_factory=list)
    verify_vision_capability: bool = True
    enable_learning: bool = True
    retention_days: int = 30

    def validate(self) -> None:
        """Raise :class:`ConfigError` when a setting cannot work as configured."""
        for name in ("llm_base_url", "media_base_url"):
            url = getattr(self, name)
            if not url.startswith(("http://", "https://")):
                raise ConfigError(f"{name} must be an http(s) URL, got {url!r}")
        if self.max_retries < 0 or self.max_retries > 10:
            raise ConfigError("max_retries must be between 0 and 10")
        if self.max_parallel < 1 or self.max_parallel > 16:
            raise ConfigError("max_parallel must be between 1 and 16")
        if self.request_timeout_s < 5 or self.request_timeout_s > 900:
            raise ConfigError("request_timeout_s must be between 5 and 900")
        if self.max_cost_per_run_usd < 0:
            raise ConfigError("max_cost_per_run_usd cannot be negative")
        if self.daily_budget_usd < 0:
            raise ConfigError("daily_budget_usd cannot be negative")
        if self.approval_threshold_usd < 0:
            raise ConfigError("approval_threshold_usd cannot be negative")
        if self.code_max_tokens < 512 or self.code_max_tokens > 65536:
            raise ConfigError("code_max_tokens must be between 512 and 65536")
        if self.max_tokens < 512 or self.max_tokens > 65536:
            raise ConfigError("max_tokens must be between 512 and 65536")
        if self.retention_days < 1:
            raise ConfigError("retention_days must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        """Return the settings for display. Never contains the credential."""
        return {
            "llm_base_url": self.llm_base_url,
            "media_base_url": self.media_base_url,
            "mcp_server": self.mcp_server,
            "use_mcp": self.use_mcp,
            "request_timeout_s": self.request_timeout_s,
            "max_retries": self.max_retries,
            "max_parallel": self.max_parallel,
            "max_cost_per_run_usd": self.max_cost_per_run_usd,
            "daily_budget_usd": self.daily_budget_usd,
            "approval_threshold_usd": self.approval_threshold_usd,
            "code_max_tokens": self.code_max_tokens,
            "max_tokens": self.max_tokens,
            "preferred_models": list(self.preferred_models),
            "blocked_models": list(self.blocked_models),
            "verify_vision_capability": self.verify_vision_capability,
            "enable_learning": self.enable_learning,
            "retention_days": self.retention_days,
        }


def _hermes_secret_reader() -> Any | None:
    """Return Hermes' ``get_secret`` when Hermes is present, else ``None``.

    ``None`` means "Hermes is not installed here" — the plugin is running
    standalone (the test suite, a script) and the process environment is the
    only source of the credential.
    """
    try:
        from agent.secret_scope import get_secret
    except ModuleNotFoundError:
        return None
    return get_secret


def read_api_key() -> str | None:
    """Return the GMI Cloud API key, or ``None`` when it is not configured.

    Inside Hermes the credential comes from Hermes' secret scope, which
    honours the active profile. The process environment is consulted **only**
    when Hermes' secret scope is not available at all — a bare
    ``os.environ`` read inside Hermes could pick up *another* profile's key
    when Hermes multiplexes profiles, which is exactly what the secret scope
    refuses to do. So when Hermes is present it is the authority: no key
    from the secret scope means no key for this plugin.
    """
    get_secret = _hermes_secret_reader()

    if get_secret is not None:
        try:
            value = get_secret(API_KEY_ENV)
        except Exception:  # pragma: no cover - Hermes could not answer
            # Hermes is present but could not resolve the credential. That
            # happens when profiles are multiplexed with no secret scope
            # bound, and the reason it raises is precisely that reading
            # os.environ here would expose another profile's value. Fail
            # closed instead.
            return None
        if value and value.strip():
            return value.strip()
        return None

    value = os.environ.get(API_KEY_ENV)
    if value and value.strip():
        return value.strip()
    return None


def require_api_key() -> str:
    """Return the API key or raise a clear, actionable error."""
    key = read_api_key()
    if not key:
        raise NotConfiguredError(
            "GMI Cloud API key is not configured. Set the GMI_API_KEY environment "
            "variable (Hermes stores it in the profile's .env) and reload the plugin."
        )
    return key


def resolve_settings(ctx: Any | None = None) -> Settings:
    """Build :class:`Settings` from the plugin context, then the environment."""
    settings = Settings()

    def cfg(key: str, default: Any = None) -> Any:
        if ctx is None:
            return default
        try:
            value = ctx.get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    settings.llm_base_url = str(cfg("llm_base_url", settings.llm_base_url)).rstrip("/")
    settings.media_base_url = str(cfg("media_base_url", settings.media_base_url)).rstrip("/")
    settings.mcp_server = str(cfg("mcp_server", settings.mcp_server) or "").strip()
    settings.use_mcp = _as_bool(cfg("use_mcp", settings.use_mcp), settings.use_mcp)
    settings.request_timeout_s = _as_int(cfg("request_timeout_s", settings.request_timeout_s), settings.request_timeout_s)
    settings.max_retries = _as_int(cfg("max_retries", settings.max_retries), settings.max_retries)
    settings.max_parallel = _as_int(cfg("max_parallel", settings.max_parallel), settings.max_parallel)
    settings.max_cost_per_run_usd = _as_float(cfg("max_cost_per_run_usd", settings.max_cost_per_run_usd), settings.max_cost_per_run_usd)
    settings.daily_budget_usd = _as_float(cfg("daily_budget_usd", settings.daily_budget_usd), settings.daily_budget_usd)
    settings.approval_threshold_usd = _as_float(cfg("approval_threshold_usd", settings.approval_threshold_usd), settings.approval_threshold_usd)
    settings.code_max_tokens = _as_int(cfg("code_max_tokens", settings.code_max_tokens), settings.code_max_tokens)
    settings.max_tokens = _as_int(cfg("max_tokens", settings.max_tokens), settings.max_tokens)
    settings.preferred_models = _as_list(cfg("preferred_models", settings.preferred_models))
    settings.blocked_models = _as_list(cfg("blocked_models", settings.blocked_models))
    settings.verify_vision_capability = _as_bool(cfg("verify_vision_capability", settings.verify_vision_capability), settings.verify_vision_capability)
    settings.enable_learning = _as_bool(cfg("enable_learning", settings.enable_learning), settings.enable_learning)
    settings.retention_days = _as_int(cfg("retention_days", settings.retention_days), settings.retention_days)

    # The provider itself publishes GMI_BASE_URL; honour it when the operator
    # has overridden the provider's base URL.
    env_base = os.environ.get(BASE_URL_ENV)
    if env_base and env_base.strip() and ctx is None:
        settings.llm_base_url = env_base.strip().rstrip("/")

    settings.validate()
    return settings
