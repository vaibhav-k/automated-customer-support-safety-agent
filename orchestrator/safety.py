"""
Layer 1 of the architecture: Azure AI Content Safety gate.

Two independent checks run on every user turn *before* the agent sees it:

1. **Prompt Shields** (``text:shieldPrompt``) — detects direct jailbreak /
   prompt-injection attacks in the user prompt and indirect attacks hidden in
   any attached documents.
2. **Text moderation** (``text:analyze``) — scores Hate, SelfHarm, Sexual and
   Violence on the 0/2/4/6 severity scale and blocks at/above a threshold.

Model output is also re-checked with text moderation (defence in depth on top
of the Foundry deployment's own content filter).

Authentication is keyless by default: a Microsoft Entra ID token for the
``https://cognitiveservices.azure.com/.default`` scope. The calling identity
needs the **Cognitive Services User** role on the Content Safety resource.

Lab fallback: pass ``api_key`` (``CONTENT_SAFETY_API_KEY``) to authenticate with the
resource key instead (``Ocp-Apim-Subscription-Key`` header) when that role cannot be
granted. Not for production.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests
from azure.ai.contentsafety import ContentSafetyClient
from azure.ai.contentsafety.models import (
    AnalyzeTextOptions,
    AnalyzeTextOutputType,
    TextCategory,
)
from azure.core.credentials import AccessToken, AzureKeyCredential, TokenCredential
from azure.core.exceptions import AzureError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("contoso.safety")

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
PROMPT_SHIELDS_API_VERSION = "2024-09-01"
PROMPT_SHIELDS_MAX_CHARS = 10_000  # per request: userPrompt + documents combined
PROMPT_SHIELDS_MAX_DOCUMENTS = 5
HARM_CATEGORIES = (
    TextCategory.HATE,
    TextCategory.SELF_HARM,
    TextCategory.SEXUAL,
    TextCategory.VIOLENCE,
)


class SafetyCategory(str, Enum):
    SAFE = "safe"
    PROMPT_INJECTION = "prompt_injection"
    DOCUMENT_INJECTION = "document_injection"
    HARMFUL_CONTENT = "harmful_content"
    INPUT_TOO_LONG = "input_too_long"
    EMPTY_INPUT = "empty_input"
    SERVICE_ERROR = "safety_service_error"


@dataclass(frozen=True)
class SafetyVerdict:
    allowed: bool
    category: SafetyCategory
    detail: str = ""
    severities: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "category": self.category.value,
            "detail": self.detail,
            "severities": dict(self.severities),
        }


def _category_name(category: object) -> str:
    """Return the wire value ("Hate", "SelfHarm", ...) for an SDK enum or plain string."""
    return str(getattr(category, "value", category))


class SafetyServiceError(RuntimeError):
    """Raised when the Content Safety service cannot produce a verdict."""


class ContentSafetyGate:
    """Pre- and post-agent safety checks backed by Azure AI Content Safety."""

    def __init__(
        self,
        endpoint: str,
        credential: TokenCredential,
        *,
        harm_severity_threshold: int = 4,
        fail_closed: bool = True,
        max_input_chars: int = 4000,
        timeout_seconds: float = 10.0,
        session: Optional[requests.Session] = None,
        text_client: Optional[ContentSafetyClient] = None,
        api_key: Optional[str] = None,
    ) -> None:
        if not endpoint:
            raise ValueError("Content Safety endpoint is required")
        self._endpoint = endpoint.rstrip("/")
        self._credential = credential
        self._threshold = harm_severity_threshold
        self._fail_closed = fail_closed
        self._max_input_chars = min(max_input_chars, PROMPT_SHIELDS_MAX_CHARS)
        self._timeout = timeout_seconds
        self._session = session or self._build_session()
        self._api_key = api_key or None
        if self._api_key:
            logger.warning(
                "Using CONTENT_SAFETY_API_KEY for Azure AI Content Safety (lab fallback). "
                "Remove it and grant 'Cognitive Services User' when possible."
            )
        client_credential: AzureKeyCredential | TokenCredential = (
            AzureKeyCredential(self._api_key) if self._api_key else credential
        )
        self._text_client = text_client or ContentSafetyClient(
            self._endpoint,
            client_credential,
            connection_timeout=5,
            read_timeout=self._timeout,
            retry_total=2,
        )
        self._token: Optional[AccessToken] = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def check_user_input(self, text: str, documents: Sequence[str] = ()) -> SafetyVerdict:
        """Run Prompt Shields + harm moderation on a user turn."""
        if not text or not text.strip():
            return SafetyVerdict(False, SafetyCategory.EMPTY_INPUT, "Input was empty.")
        if len(text) > self._max_input_chars:
            return SafetyVerdict(
                False,
                SafetyCategory.INPUT_TOO_LONG,
                f"Input exceeds the {self._max_input_chars}-character limit.",
            )
        if (
            len(documents) > PROMPT_SHIELDS_MAX_DOCUMENTS
            or len(text) + sum(len(d) for d in documents) > PROMPT_SHIELDS_MAX_CHARS
        ):
            return SafetyVerdict(
                False,
                SafetyCategory.INPUT_TOO_LONG,
                f"Attachments exceed the limit of {PROMPT_SHIELDS_MAX_DOCUMENTS} documents / "
                f"{PROMPT_SHIELDS_MAX_CHARS} characters in total.",
            )
        try:
            shield = self._shield_prompt(text, documents)
            if shield.get("userPromptAnalysis", {}).get("attackDetected"):
                logger.warning("Prompt Shields detected a user prompt attack")
                return SafetyVerdict(
                    False,
                    SafetyCategory.PROMPT_INJECTION,
                    "Prompt Shields detected a jailbreak/prompt-injection attempt.",
                )
            for index, doc in enumerate(shield.get("documentsAnalysis", []) or []):
                # Prompt Shields only detects attacks; attachments are also harm-moderated below.
                if doc.get("attackDetected"):
                    logger.warning(
                        "Prompt Shields detected an indirect attack in document %d",
                        index,
                    )
                    return SafetyVerdict(
                        False,
                        SafetyCategory.DOCUMENT_INJECTION,
                        f"Prompt Shields detected an indirect attack in document {index}.",
                    )
            verdict = self._moderate(text)
            if not verdict.allowed:
                return verdict
            for doc in documents:
                doc_verdict = self._moderate(doc)
                if not doc_verdict.allowed:
                    return doc_verdict
            return verdict
        except SafetyServiceError as exc:
            return self._on_service_error(exc)

    def check_model_output(self, text: str) -> SafetyVerdict:
        """Run harm moderation on the agent's reply before it is shown to the user."""
        if not text or not text.strip():
            return SafetyVerdict(True, SafetyCategory.SAFE, "Empty output.")
        try:
            return self._moderate(text[:PROMPT_SHIELDS_MAX_CHARS])
        except SafetyServiceError as exc:
            return self._on_service_error(exc)

    def close(self) -> None:
        self._session.close()
        self._text_client.close()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_session() -> requests.Session:
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True,
        )
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _auth_headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Ocp-Apim-Subscription-Key": self._api_key}
        return {"Authorization": f"Bearer {self._bearer_token()}"}

    def _bearer_token(self) -> str:
        if self._token is None or self._token.expires_on - 120 <= time.time():
            self._token = self._credential.get_token(COGNITIVE_SERVICES_SCOPE)
        return self._token.token

    def _shield_prompt(self, text: str, documents: Sequence[str]) -> dict:
        url = f"{self._endpoint}/contentsafety/text:shieldPrompt"
        body = {"userPrompt": text, "documents": list(documents)}
        try:
            response = self._session.post(
                url,
                params={"api-version": PROMPT_SHIELDS_API_VERSION},
                json=body,
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except (requests.RequestException, AzureError) as exc:
            raise SafetyServiceError(f"Prompt Shields request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise SafetyServiceError(f"Prompt Shields returned HTTP {response.status_code}: {self._auth_hint()}")
        if response.status_code != 200:
            raise SafetyServiceError(f"Prompt Shields returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise SafetyServiceError("Prompt Shields returned a non-JSON body") from exc

    def _moderate(self, text: str) -> SafetyVerdict:
        try:
            result = self._text_client.analyze_text(
                AnalyzeTextOptions(
                    text=text,
                    categories=list(HARM_CATEGORIES),
                    output_type=AnalyzeTextOutputType.FOUR_SEVERITY_LEVELS,
                )
            )
        except AzureError as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise SafetyServiceError(f"Text moderation returned HTTP {status}: {self._auth_hint()}") from exc
            raise SafetyServiceError(f"Text moderation failed: {exc}") from exc

        severities = {
            _category_name(item.category): int(item.severity or 0) for item in (result.categories_analysis or [])
        }
        flagged = {cat: sev for cat, sev in severities.items() if sev >= self._threshold}
        if flagged:
            logger.warning("Harmful content blocked: %s", flagged)
            return SafetyVerdict(
                False,
                SafetyCategory.HARMFUL_CONTENT,
                "Content exceeded the harm severity threshold: "
                + ", ".join(f"{c}={s}" for c, s in sorted(flagged.items())),
                severities,
            )
        return SafetyVerdict(True, SafetyCategory.SAFE, "", severities)

    def _auth_hint(self) -> str:
        if self._api_key:
            return (
                "the CONTENT_SAFETY_API_KEY was rejected; "
                "copy KEY 1 from the Content Safety resource > Keys and Endpoint."
            )
        return (
            "your identity lacks the 'Cognitive Services User' role on the Content Safety resource "
            "(ask an admin, or set CONTENT_SAFETY_API_KEY as a lab fallback)."
        )

    def _on_service_error(self, exc: SafetyServiceError) -> SafetyVerdict:
        logger.error("Content Safety unavailable: %s", exc)
        if self._fail_closed:
            return SafetyVerdict(
                False,
                SafetyCategory.SERVICE_ERROR,
                "Safety service unavailable; request blocked (fail-closed).",
            )
        return SafetyVerdict(
            True,
            SafetyCategory.SERVICE_ERROR,
            "Safety service unavailable; request allowed (fail-open).",
        )
