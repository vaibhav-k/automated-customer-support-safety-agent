"""Shared Microsoft Entra ID credential factory (keyless auth for every client in this repo)."""

from __future__ import annotations

from azure.identity import DefaultAzureCredential

# `az` on Windows can take longer than DefaultAzureCredential's 10 s default to start, which surfaces as a
# misleading "AzureCliCredential: credential unavailable". 30 s avoids that without hiding real failures.
CLI_PROCESS_TIMEOUT_SECONDS = 30


def make_credential() -> DefaultAzureCredential:
    """Non-interactive credential: environment / managed identity / Azure CLI / PowerShell / azd."""
    return DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        process_timeout=CLI_PROCESS_TIMEOUT_SECONDS,
    )
