"""Centralised, validated configuration loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
DATA_DIR = REPO_ROOT / "data"

SYSTEM_PROMPT_PATH = AGENT_DIR / "system_prompt.txt"
OPENAPI_SPEC_PATH = AGENT_DIR / "openapi_spec.json"
POLICY_PATH = DATA_DIR / "contoso_policy.md"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_optional_bool(name: str) -> Optional[bool]:
    """Like _env_bool, but unset/empty means "not specified" (None)."""
    if _env(name) is None:
        return None
    return _env_bool(name, False)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _parse_temperature(raw: Optional[str]) -> Optional[float]:
    """Empty/unset/"none" -> None (omit the parameter). Reasoning models (o-series, gpt-5 family,
    "chat-latest" aliases) reject ``temperature`` entirely, so omitting it is the safe default.
    """
    if raw is None or raw.lower() in {"none", "default"}:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"AGENT_TEMPERATURE must be a number or empty, got {raw!r}") from exc
    if not 0.0 <= value <= 2.0:
        raise ConfigError("AGENT_TEMPERATURE must be between 0.0 and 2.0")
    return value


@dataclass(frozen=True)
class Settings:
    # Foundry project + agent
    foundry_project_endpoint: Optional[str]
    model_deployment_name: str
    agent_name: str
    agent_temperature: Optional[float]  # None = model default (required for reasoning models)

    # Azure AI Content Safety
    content_safety_endpoint: Optional[str]
    content_safety_api_key: Optional[str]  # lab fallback only; prefer Entra ID (Cognitive Services User)
    harm_severity_threshold: int
    safety_fail_closed: bool
    max_input_chars: int

    # Azure AI Search + embeddings (ingestion)
    search_endpoint: Optional[str]
    search_index_name: str
    search_connection_name: Optional[str]
    search_api_key: Optional[str]  # lab fallback only; prefer Entra ID (RBAC)
    vectorizer_api_key: Optional[str]  # lab fallback only; prefer the search service's managed identity
    search_top_k: int
    azure_openai_endpoint: Optional[str]
    embedding_deployment_name: str
    embedding_model_name: str
    embedding_dimensions: int
    policy_base_url: str

    # Order API (Azure Function) tool
    order_api_base_url: Optional[str]
    order_api_connection_name: Optional[str]

    # Evaluation judges (scripts/evaluate_quality.py)
    eval_model_deployment_name: Optional[str]
    eval_is_reasoning_model: Optional[bool]  # None = auto-detect from the deployment name

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> Settings:
        load_dotenv(env_file or REPO_ROOT / ".env", override=False)
        temperature = _parse_temperature(_env("AGENT_TEMPERATURE"))

        return cls(
            foundry_project_endpoint=_env("FOUNDRY_PROJECT_ENDPOINT"),
            model_deployment_name=_env("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini"),  # type: ignore[arg-type]
            agent_name=_env("AGENT_NAME", "contoso-support-agent"),  # type: ignore[arg-type]
            agent_temperature=temperature,
            content_safety_endpoint=_env("CONTENT_SAFETY_ENDPOINT"),
            content_safety_api_key=_env("CONTENT_SAFETY_API_KEY"),
            harm_severity_threshold=_env_int("HARM_SEVERITY_THRESHOLD", 4, 1, 7),
            safety_fail_closed=_env_bool("SAFETY_FAIL_CLOSED", True),
            max_input_chars=_env_int("MAX_INPUT_CHARS", 4000, 100, 10000),
            search_endpoint=_env("AZURE_SEARCH_ENDPOINT"),
            search_index_name=_env("AZURE_SEARCH_INDEX_NAME", "contoso-policy-index"),  # type: ignore[arg-type]
            search_connection_name=_env("AZURE_SEARCH_CONNECTION_NAME"),
            search_api_key=_env("AZURE_SEARCH_API_KEY"),
            vectorizer_api_key=_env("AZURE_OPENAI_VECTORIZER_API_KEY"),
            search_top_k=_env_int("AZURE_SEARCH_TOP_K", 5, 1, 20),
            azure_openai_endpoint=_env("AZURE_OPENAI_ENDPOINT"),
            embedding_deployment_name=_env("EMBEDDING_DEPLOYMENT_NAME", "text-embedding-3-small"),  # type: ignore[arg-type]
            embedding_model_name=_env("EMBEDDING_MODEL_NAME", "text-embedding-3-small"),  # type: ignore[arg-type]
            embedding_dimensions=_env_int("EMBEDDING_DIMENSIONS", 1536, 256, 3072),
            policy_base_url=_env("POLICY_BASE_URL", "https://policies.contoso.example/contoso_policy.md"),  # type: ignore[arg-type]
            order_api_base_url=_env("ORDER_API_BASE_URL"),
            order_api_connection_name=_env("ORDER_API_CONNECTION_NAME"),
            eval_model_deployment_name=_env("EVAL_MODEL_DEPLOYMENT_NAME"),
            eval_is_reasoning_model=_env_optional_bool("EVAL_IS_REASONING_MODEL"),
        )

    def __repr__(self) -> str:  # never print secrets in logs/tracebacks
        masked = {
            f.name: ("***" if f.name.endswith("api_key") and getattr(self, f.name) else getattr(self, f.name))
            for f in fields(self)
        }
        return f"Settings({masked})"

    def required_str(self, attribute_name: str) -> str:
        """Return a required string setting, typed as ``str`` (raises ConfigError if missing)."""
        self.require(attribute_name)
        value = getattr(self, attribute_name)
        if not isinstance(value, str):
            raise ConfigError(f"Setting {attribute_name!r} is not a string")
        return value

    def require(self, *attribute_names: str) -> None:
        """Fail fast with one clear message listing every missing setting."""
        known = {f.name for f in fields(self)}
        missing: list[str] = []
        for name in attribute_names:
            if name not in known:
                raise ConfigError(f"Unknown setting {name!r}")
            if getattr(self, name) in (None, ""):
                missing.append(name.upper())
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(_ENV_NAME_OVERRIDES.get(m, m) for m in missing)
                + ". Set them in your environment or in the .env file (see .env.example)."
            )


# Attribute names that differ from their environment variable names.
_ENV_NAME_OVERRIDES = {
    "MODEL_DEPLOYMENT_NAME": "FOUNDRY_MODEL_DEPLOYMENT_NAME",
    "SEARCH_ENDPOINT": "AZURE_SEARCH_ENDPOINT",
    "SEARCH_INDEX_NAME": "AZURE_SEARCH_INDEX_NAME",
    "SEARCH_CONNECTION_NAME": "AZURE_SEARCH_CONNECTION_NAME",
}
