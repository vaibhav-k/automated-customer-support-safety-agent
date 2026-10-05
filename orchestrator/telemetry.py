"""
Optional OpenTelemetry tracing for the support pipeline.

Three modes (``TRACING_MODE`` in .env, or ``--trace`` on the CLIs):

* ``off`` (default)  - no provider is installed; every span below is a free no-op.
* ``console``        - a compact span tree per turn printed to stderr. No Azure resources needed.
* ``azure_monitor``  - export to the Application Insights resource connected to the Foundry project,
                       so traces show up in **Foundry portal > Tracing** and in App Insights.

What gets traced:

* ``contoso.pipeline.turn`` (root span per customer turn, created by :mod:`orchestrator.pipeline`)
  with child spans ``contoso.safety.input`` / ``contoso.safety.output`` for the Content Safety gate.
* The Foundry agent call: ``AIProjectInstrumentor`` patches the OpenAI ``responses`` and
  ``conversations`` APIs and emits OpenTelemetry GenAI spans (``gen_ai.*`` attributes: agent, model,
  token usage, tool calls). It also propagates the W3C trace context to the Agent Service so the
  server-side spans join the same trace.
* Azure SDK / HTTP calls (Content Safety, Prompt Shields) as dependency spans.

Message content (prompts, replies, tool arguments) is NOT recorded unless
``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`` - it can contain personal data.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import IO, Any, Optional

from azure.core.credentials import TokenCredential
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import StatusCode

from .config import ConfigError, Settings

logger = logging.getLogger("contoso.telemetry")

TRACER_NAME = "contoso.support"
DEFAULT_SERVICE_NAME = "contoso-support-orchestrator"
# Required by azure-ai-projects: GenAI tracing is a preview feature behind this switch.
GENAI_TRACING_SWITCH = "AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"


class TracingMode(str, Enum):
    OFF = "off"
    CONSOLE = "console"
    AZURE_MONITOR = "azure_monitor"

    @classmethod
    def parse(cls, raw: Optional[str]) -> TracingMode:
        """Accept ``off|console|azure_monitor`` plus friendly aliases; empty means ``off``."""
        if raw is None or not raw.strip():
            return cls.OFF
        value = raw.strip().lower().replace("-", "_")
        aliases = {
            "none": cls.OFF,
            "false": cls.OFF,
            "0": cls.OFF,
            "stdout": cls.CONSOLE,
            "appinsights": cls.AZURE_MONITOR,
            "app_insights": cls.AZURE_MONITOR,
            "application_insights": cls.AZURE_MONITOR,
            "azuremonitor": cls.AZURE_MONITOR,
        }
        if value in aliases:
            return aliases[value]
        try:
            return cls(value)
        except ValueError as exc:
            choices = ", ".join(m.value for m in cls)
            raise ConfigError(f"TRACING_MODE must be one of {choices}, got {raw!r}") from exc


def get_tracer() -> trace.Tracer:
    """The tracer for this app's own spans (a no-op until :func:`configure_tracing` installs a provider)."""
    return trace.get_tracer(TRACER_NAME)


def current_trace_id() -> str:
    """Hex trace id of the active span ('' when tracing is off). Matches ``operation_Id`` in App Insights."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else ""


# --------------------------------------------------------------------------------------------------
# Console exporter: one readable tree per trace instead of the SDK's multi-page JSON dump.
# --------------------------------------------------------------------------------------------------

# span attribute -> short label shown by the console exporter
_CONSOLE_ATTRIBUTES = {
    "contoso.case.id": "case",
    "contoso.stage": "stage",
    "contoso.blocked": "blocked",
    "contoso.safety.allowed": "allowed",
    "contoso.safety.category": "category",
    "contoso.tool_calls": "tools",
    "contoso.citations": "citations",
    "gen_ai.usage.input_tokens": "in_tokens",
    "gen_ai.usage.output_tokens": "out_tokens",
    "http.response.status_code": "http",
    "http.status_code": "http",
    "error.type": "error",
}


def _format_value(key: str, value: Any) -> str:
    if key == "error.type":  # "azure.core.exceptions.HttpResponseError" -> "HttpResponseError"
        return str(value).rsplit(".", 1)[-1]
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) or "-"
    return str(value)


def format_span_line(span: ReadableSpan, depth: int) -> str:
    """``<indent><name>  <ms> ms  key=value ...`` (pure function, unit-tested)."""
    start, end = span.start_time or 0, span.end_time or 0
    duration_ms = max(end - start, 0) / 1_000_000
    attributes = span.attributes or {}
    details = [
        f"{label}={_format_value(key, attributes[key])}"
        for key, label in _CONSOLE_ATTRIBUTES.items()
        if key in attributes
    ]
    status = " ERROR" if span.status.status_code is StatusCode.ERROR else ""
    line = f"{'  ' * depth}{span.name}  {duration_ms:.0f} ms{status}"
    return f"{line}  {' '.join(details)}" if details else line


class CompactConsoleExporter(SpanExporter):
    """Buffers spans per trace; when the root ends, prints the tree for this app's ``contoso.*`` spans and a
    single line for anything else (SDK setup calls such as ``AgentsOperations.get``)."""

    def __init__(self, stream: Optional[IO[str]] = None) -> None:
        self._stream = stream
        self._pending: dict[int, list[ReadableSpan]] = {}
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for span in spans:
                if span.context is None:
                    continue
                self._pending.setdefault(span.context.trace_id, []).append(span)
                if span.parent is None or span.parent.is_remote:
                    trace_spans = self._pending.pop(span.context.trace_id)
                    if span.name.startswith("contoso."):
                        self._write_tree(trace_spans, span)
                    else:
                        self._write(f"{format_span_line(span, 0)}  trace={span.context.trace_id:032x}")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        with self._lock:  # print spans whose root never ended (e.g. interrupted run)
            for spans in self._pending.values():
                for span in spans:
                    self._write(format_span_line(span, 0))
            self._pending.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def _write_tree(self, spans: list[ReadableSpan], root: ReadableSpan) -> None:
        children: dict[int, list[ReadableSpan]] = {}
        for span in spans:
            if span is not root and span.parent is not None:
                children.setdefault(span.parent.span_id, []).append(span)
        trace_id = format(root.context.trace_id, "032x") if root.context else "?"
        lines = [f"[trace {trace_id}]"]
        self._append_subtree(root, 0, children, lines)
        self._write("\n".join(lines))

    def _append_subtree(
        self,
        span: ReadableSpan,
        depth: int,
        children: dict[int, list[ReadableSpan]],
        lines: list[str],
    ) -> None:
        lines.append(format_span_line(span, depth))
        span_id = span.context.span_id if span.context else None
        for child in sorted(
            children.get(span_id, []) if span_id else [],
            key=lambda s: s.start_time or 0,
        ):
            self._append_subtree(child, depth + 1, children, lines)

    def _write(self, text: str) -> None:
        stream = self._stream or sys.stderr
        stream.write(text + "\n")
        stream.flush()


# --------------------------------------------------------------------------------------------------
# Setup / teardown
# --------------------------------------------------------------------------------------------------


@dataclass
class TracingHandle:
    """What :func:`configure_tracing` set up; call :meth:`shutdown` before exit so no spans are lost."""

    mode: TracingMode
    capture_content: bool = False
    _provider: Optional[TracerProvider] = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return self.mode is not TracingMode.OFF

    def shutdown(self) -> None:
        if self._provider is None:
            return
        try:
            self._provider.force_flush()  # Azure Monitor batches spans; a short CLI run would drop them
            self._provider.shutdown()
        except Exception as exc:  # telemetry must never break the app on exit
            logger.warning("Tracing shutdown failed: %s", exc)
        finally:
            self._provider = None


def _resource() -> Resource:
    # OTEL_SERVICE_NAME (if set) wins; otherwise use a stable, descriptive default.
    return Resource.create({} if os.environ.get("OTEL_SERVICE_NAME") else {SERVICE_NAME: DEFAULT_SERVICE_NAME})


def project_connection_string(project_endpoint: str, credential: TokenCredential) -> str:
    """Read the Application Insights connection string from the Foundry project's connection."""
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import AzureError, ResourceNotFoundError

    with AIProjectClient(endpoint=project_endpoint, credential=credential) as project:
        try:
            return project.telemetry.get_application_insights_connection_string()
        except ResourceNotFoundError as exc:
            raise ConfigError(
                "No Application Insights resource is connected to the Foundry project. Connect one in the "
                "Foundry portal (Tracing > Connect), or set APPLICATIONINSIGHTS_CONNECTION_STRING."
            ) from exc
        except (
            AzureError,
            ValueError,
        ) as exc:  # 401/403, network/DNS, or a non-key connection
            status = getattr(exc, "status_code", None)
            raise ConfigError(
                f"Could not read the project's Application Insights connection ({status or exc}). Set "
                "APPLICATIONINSIGHTS_CONNECTION_STRING from Application Insights > Overview > Connection String."
            ) from exc


def _install_console(stream: Optional[IO[str]]) -> TracerProvider:
    from azure.core.settings import settings as azure_core_settings

    provider = TracerProvider(resource=_resource())
    provider.add_span_processor(SimpleSpanProcessor(CompactConsoleExporter(stream)))
    trace.set_tracer_provider(provider)
    azure_core_settings.tracing_implementation = "opentelemetry"  # Azure SDK calls become child spans
    return provider


def _install_azure_monitor(connection_string: str) -> Optional[TracerProvider]:
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=connection_string,
        resource=_resource(),
        logger_name="contoso",  # export this app's warnings/errors as traces, not every SDK debug line
    )
    provider = trace.get_tracer_provider()
    return provider if isinstance(provider, TracerProvider) else None


def configure_tracing(
    mode: TracingMode,
    *,
    project_endpoint: Optional[str] = None,
    credential: Optional[TokenCredential] = None,
    connection_string: Optional[str] = None,
    capture_content: bool = False,
    stream: Optional[IO[str]] = None,
) -> TracingHandle:
    """Install the tracer provider for ``mode`` and instrument the Foundry SDK.

    Call this BEFORE creating ``SupportAgentClient``: trace-context propagation is attached to the
    OpenAI client when ``get_openai_client()`` runs.
    """
    if mode is TracingMode.OFF:
        return TracingHandle(mode)
    # Set first: configure_azure_monitor() may already load the azure-ai-projects instrumentor, which is a
    # no-op (with a warning) unless this preview switch is on.
    os.environ[GENAI_TRACING_SWITCH] = "true"

    if mode is TracingMode.CONSOLE:
        provider: Optional[TracerProvider] = _install_console(stream)
    else:
        if not connection_string:
            if not project_endpoint or credential is None:
                raise ConfigError(
                    "TRACING_MODE=azure_monitor needs FOUNDRY_PROJECT_ENDPOINT or APPLICATIONINSIGHTS_CONNECTION_STRING"
                )
            connection_string = project_connection_string(project_endpoint, credential)
        provider = _install_azure_monitor(connection_string)

    from azure.ai.projects.telemetry import AIProjectInstrumentor

    AIProjectInstrumentor().instrument(enable_content_recording=capture_content)
    if capture_content:
        logger.warning("Trace content recording is ON: prompts and replies are stored in your traces.")
    logger.info("Tracing enabled (%s)", mode.value)
    return TracingHandle(mode, capture_content, provider)


def configure_from_settings(
    settings: Settings,
    credential: Optional[TokenCredential],
    *,
    mode_override: Optional[str] = None,
    stream: Optional[IO[str]] = None,
) -> TracingHandle:
    """:func:`configure_tracing` driven by ``.env``; ``mode_override`` is the CLI's ``--trace`` value."""
    mode = TracingMode.parse(mode_override if mode_override is not None else settings.tracing_mode)
    return configure_tracing(
        mode,
        project_endpoint=settings.foundry_project_endpoint,
        credential=credential,
        connection_string=settings.applicationinsights_connection_string,
        capture_content=settings.trace_capture_content,
        stream=stream,
    )
