"""Remove what this project created inside Azure, then print the commands that delete the Azure resources.

What it deletes (only with ``--yes``; without it, the script only shows the plan):

* the Foundry **agent** ``AGENT_NAME`` with **all of its versions**,
* the Azure AI Search **index** ``AZURE_SEARCH_INDEX_NAME``.

What it does not delete:

* **Conversations.** The Conversations API has no "list" operation, so orphans can't be found. The CLI and
  ``run_exam_checks`` delete every conversation they create when they finish; anything left by a crashed run
  expires under the project's data-retention policy and disappears with the Foundry resource.
* **Azure resources and resource groups.** Deleting those is irreversible, so the script prints the
  ``az group delete`` commands for you to run yourself (set ``TEARDOWN_RESOURCE_GROUPS`` to fill them in).

Usage (from the repo root)::

    python -m scripts.teardown                 # dry run: show what would be deleted
    python -m scripts.teardown --yes           # delete the agent and the index
    python -m scripts.teardown --yes --keep-index
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable

from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError

from orchestrator.auth import make_credential
from orchestrator.config import ConfigError, Settings

logger = logging.getLogger("contoso.teardown")

Step = Callable[[], str]


def _delete_agent(settings: Settings, credential: TokenCredential) -> str:
    from azure.ai.projects import AIProjectClient

    with AIProjectClient(endpoint=settings.required_str("foundry_project_endpoint"), credential=credential) as project:
        try:
            project.agents.delete(agent_name=settings.agent_name)
        except ResourceNotFoundError:
            return f"agent '{settings.agent_name}' not found (already deleted)"
    return f"deleted agent '{settings.agent_name}' and all of its versions"


def _delete_index(settings: Settings, credential: TokenCredential) -> str:
    from azure.search.documents.indexes import SearchIndexClient

    search_credential: AzureKeyCredential | TokenCredential = (
        AzureKeyCredential(settings.search_api_key) if settings.search_api_key else credential
    )
    with SearchIndexClient(settings.required_str("search_endpoint"), search_credential) as client:
        try:
            client.delete_index(settings.search_index_name)
        except ResourceNotFoundError:
            return f"index '{settings.search_index_name}' not found (already deleted)"
    return f"deleted index '{settings.search_index_name}'"


def plan(settings: Settings, keep_agent: bool, keep_index: bool) -> list[str]:
    """Human-readable list of what --yes would delete (pure; used for the dry run and tests)."""
    items = []
    if not keep_agent:
        items.append(f"Foundry agent '{settings.agent_name}' (all versions)")
    if not keep_index:
        items.append(f"Azure AI Search index '{settings.search_index_name}'")
    return items


def resource_group_commands(groups: list[str]) -> list[str]:
    targets = groups or ["<resource-group>"]
    commands = [f"az group delete --name {group} --yes --no-wait" for group in targets]
    commands.append("# Foundry resources are soft-deleted; purge to reuse the name:")
    commands.append(
        "# Azure portal > Microsoft Foundry > Manage deleted resources > Purge"
        "  (or: az cognitiveservices account purge --name <foundry> --resource-group <rg> --location <region>)"
    )
    return commands


def _run_step(label: str, step: Step) -> bool:
    try:
        print(f"  - {step()}")
        return True
    except HttpResponseError as exc:
        print(f"  ! {label} failed (HTTP {exc.status_code}): {exc.message}", file=sys.stderr)
    except AzureError as exc:
        print(f"  ! {label} failed: {exc}", file=sys.stderr)
    return False


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete the agent and index created by this project.")
    parser.add_argument("--yes", action="store_true", help="Actually delete (default is a dry run).")
    parser.add_argument("--keep-agent", action="store_true", help="Do not delete the Foundry agent.")
    parser.add_argument("--keep-index", action="store_true", help="Do not delete the search index.")
    return parser.parse_args(argv)


def _load_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    required: list[str] = []
    if not args.keep_agent:
        required += ["foundry_project_endpoint", "agent_name"]
    if not args.keep_index:
        required += ["search_endpoint", "search_index_name"]
    settings.require(*required)
    return settings


def _delete_selected(settings: Settings, args: argparse.Namespace) -> bool:
    print("Deleting:")
    credential = make_credential()
    ok = True
    try:
        if not args.keep_agent:
            ok &= _run_step("agent", lambda: _delete_agent(settings, credential))
        if not args.keep_index:
            ok &= _run_step("index", lambda: _delete_index(settings, credential))
    finally:
        credential.close()
    return ok


def _print_dry_run(items: list[str]) -> None:
    print("Dry run - would delete:" if items else "Nothing selected to delete.")
    for item in items:
        print(f"  - {item}")
    print("Re-run with --yes to delete.")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(name)s: %(message)s")
    for noisy in ("azure", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:
        settings = _load_settings(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    items = plan(settings, args.keep_agent, args.keep_index)
    ok = True
    if not args.yes:
        _print_dry_run(items)
    elif items:
        ok = _delete_selected(settings, args)

    groups = [g.strip() for g in os.environ.get("TEARDOWN_RESOURCE_GROUPS", "").split(",") if g.strip()]
    print("\nWhen you are done studying, delete the Azure resources (irreversible - run these yourself):")
    for command in resource_group_commands(groups):
        print(f"  {command}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
