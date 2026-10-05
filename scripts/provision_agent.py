"""Create a new version of the Contoso support agent in Azure AI Foundry.

The agent definition combines:
* ``agent/system_prompt.txt``  -> instructions (persona, guardrails, grounding, fallbacks)
* Azure AI Search tool         -> RAG over the ``contoso-policy-index`` (vector + semantic hybrid)
* OpenAPI tool                 -> ``agent/openapi_spec.json`` pointing at the Azure Function

Usage (from the repo root)::

    python -m scripts.provision_agent --dry-run   # print the definition, no Azure calls
    python -m scripts.provision_agent             # create a new agent version

Running it again creates a new *version* of the same agent name, which is how
prompt/tool changes are rolled out (and rolled back) in Foundry.

Required role for the identity running this script: **Azure AI User** (or higher,
e.g. Azure AI Project Manager) on the Foundry project.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("contoso.provision")

OPENAPI_TOOL_NAME = "contoso_order_status"
OPENAPI_TOOL_DESCRIPTION = (
    "Look up the live status of a Contoso customer's order (Processing, Shipped, Delivered, "
    "Cancelled, ReturnInitiated, Refunded) by customerId in the format CUST-12345."
)
DRY_RUN_PLACEHOLDER = "<resolved-at-runtime>"


def prepare_openapi_spec(spec: dict[str, Any], base_url: str, use_connection_auth: bool) -> dict[str, Any]:
    """Point the spec at the deployed Function App and align security with the auth mode."""
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(
            "ORDER_API_BASE_URL must be a public https:// URL (e.g. https://<app>.azurewebsites.net/api); "
            "the Foundry service cannot reach localhost"
        )
    prepared = copy.deepcopy(spec)
    prepared["servers"] = [
        {
            "url": base_url.rstrip("/"),
            "description": "Contoso Order API (Azure Functions)",
        }
    ]
    if not use_connection_auth:
        # Anonymous auth: the tool must not advertise a security requirement it cannot satisfy.
        prepared.pop("security", None)
        prepared.get("components", {}).pop("securitySchemes", None)
        for path_item in prepared.get("paths", {}).values():
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation.pop("security", None)
    return prepared


def build_definition(
    *,
    model: str,
    instructions: str,
    temperature: float,
    search_connection_id: str,
    index_name: str,
    top_k: int,
    openapi_spec: dict[str, Any],
    order_api_connection_id: Optional[str],
):
    from azure.ai.projects.models import (
        AISearchIndexResource,
        AzureAISearchQueryType,
        AzureAISearchTool,
        AzureAISearchToolResource,
        OpenApiAnonymousAuthDetails,
        OpenApiAuthDetails,
        OpenApiFunctionDefinition,
        OpenApiProjectConnectionAuthDetails,
        OpenApiProjectConnectionSecurityScheme,
        OpenApiTool,
        PromptAgentDefinition,
    )

    search_tool = AzureAISearchTool(
        azure_ai_search=AzureAISearchToolResource(
            indexes=[
                AISearchIndexResource(
                    project_connection_id=search_connection_id,
                    index_name=index_name,
                    query_type=AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID,
                    top_k=top_k,
                )
            ]
        )
    )
    auth: OpenApiAuthDetails
    if order_api_connection_id:
        auth = OpenApiProjectConnectionAuthDetails(
            security_scheme=OpenApiProjectConnectionSecurityScheme(project_connection_id=order_api_connection_id)
        )
    else:
        auth = OpenApiAnonymousAuthDetails()
    openapi_tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name=OPENAPI_TOOL_NAME,
            description=OPENAPI_TOOL_DESCRIPTION,
            spec=openapi_spec,
            auth=auth,
        )
    )
    return PromptAgentDefinition(
        model=model,
        instructions=instructions,
        temperature=temperature,
        tools=[search_tool, openapi_tool],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the Contoso support agent in Foundry.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the definition without calling Azure.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Keep our INFO messages but silence per-request HTTP/credential logging from the Azure SDKs.
    for noisy in ("azure", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from orchestrator.config import (
        OPENAPI_SPEC_PATH,
        SYSTEM_PROMPT_PATH,
        ConfigError,
        Settings,
    )

    try:
        settings = Settings.from_env()
        required = [
            "agent_name",
            "model_deployment_name",
            "search_index_name",
            "order_api_base_url",
        ]
        if not args.dry_run:
            required += ["foundry_project_endpoint", "search_connection_name"]
        settings.require(*required)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    instructions = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    raw_spec = json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8"))
    use_connection = bool(settings.order_api_connection_name)
    try:
        spec = prepare_openapi_spec(raw_spec, settings.order_api_base_url, use_connection)  # type: ignore[arg-type]
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        definition = build_definition(
            model=settings.model_deployment_name,
            instructions=instructions,
            temperature=settings.agent_temperature,
            search_connection_id=DRY_RUN_PLACEHOLDER,
            index_name=settings.search_index_name,
            top_k=settings.search_top_k,
            openapi_spec=spec,
            order_api_connection_id=DRY_RUN_PLACEHOLDER if use_connection else None,
        )
        print(
            json.dumps(
                {"agent_name": settings.agent_name, "definition": definition.as_dict()},
                indent=2,
            )
        )
        return 0

    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    project = AIProjectClient(endpoint=settings.foundry_project_endpoint, credential=credential)  # type: ignore[arg-type]
    try:
        try:
            search_connection_id = project.connections.get(settings.search_connection_name).id  # type: ignore[arg-type]
            order_connection_id = (
                project.connections.get(settings.order_api_connection_name).id if use_connection else None  # type: ignore[arg-type]
            )
        except ResourceNotFoundError as exc:
            print(
                "Project connection not found. Check AZURE_SEARCH_CONNECTION_NAME / "
                f"ORDER_API_CONNECTION_NAME in Foundry > Management center > Connected resources. ({exc.message})",
                file=sys.stderr,
            )
            return 2

        definition = build_definition(
            model=settings.model_deployment_name,
            instructions=instructions,
            temperature=settings.agent_temperature,
            search_connection_id=search_connection_id,
            index_name=settings.search_index_name,
            top_k=settings.search_top_k,
            openapi_spec=spec,
            order_api_connection_id=order_connection_id,
        )
        agent = project.agents.create_version(
            agent_name=settings.agent_name,
            definition=definition,
            description="Contoso customer support agent with RAG (Azure AI Search) and order-status tool.",
            metadata={"project": "ai-103-catch-all", "owner": "customer-support"},
        )
        logger.info("Agent ready: name=%s version=%s id=%s", agent.name, agent.version, agent.id)
        print(json.dumps({"name": agent.name, "version": agent.version, "id": agent.id}))
        return 0
    except HttpResponseError as exc:
        logger.error("Foundry rejected the agent definition: %s", exc.message)
        return 1
    finally:
        project.close()
        credential.close()


if __name__ == "__main__":
    sys.exit(main())
