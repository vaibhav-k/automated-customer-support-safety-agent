"""Offline tests for scripts/teardown.py (no Azure calls)."""

from azure.core.exceptions import HttpResponseError

from orchestrator.config import Settings
from scripts import teardown


def _settings(monkeypatch) -> Settings:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://x.services.ai.azure.com/api/projects/p")
    monkeypatch.setenv("AGENT_NAME", "contoso-support-agent")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://s.search.windows.net")
    monkeypatch.setenv("AZURE_SEARCH_INDEX_NAME", "contoso-policy-index")
    return Settings.from_env()


def test_plan_lists_selected_items(monkeypatch):
    settings = _settings(monkeypatch)
    assert teardown.plan(settings, keep_agent=False, keep_index=False) == [
        "Foundry agent 'contoso-support-agent' (all versions)",
        "Azure AI Search index 'contoso-policy-index'",
    ]
    assert teardown.plan(settings, keep_agent=True, keep_index=True) == []


def test_resource_group_commands():
    commands = teardown.resource_group_commands(["rg-a", "rg-b"])
    assert commands[:2] == [
        "az group delete --name rg-a --yes --no-wait",
        "az group delete --name rg-b --yes --no-wait",
    ]
    assert "<resource-group>" in teardown.resource_group_commands([])[0]


def test_dry_run_deletes_nothing(monkeypatch, capsys):
    _settings(monkeypatch)
    monkeypatch.setenv("TEARDOWN_RESOURCE_GROUPS", "rg-one")

    def explode(*_args, **_kwargs):
        raise AssertionError("dry run must not delete")

    monkeypatch.setattr(teardown, "_delete_agent", explode)
    monkeypatch.setattr(teardown, "_delete_index", explode)
    assert teardown.main([]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "az group delete --name rg-one" in out


def test_yes_runs_steps_and_reports_failures(monkeypatch, capsys):
    _settings(monkeypatch)
    monkeypatch.setattr(teardown, "make_credential", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(teardown, "_delete_agent", lambda s, c: "deleted agent 'contoso-support-agent'")

    def forbidden(_settings, _credential):
        raise HttpResponseError(message="Forbidden")

    monkeypatch.setattr(teardown, "_delete_index", forbidden)
    assert teardown.main(["--yes"]) == 1
    captured = capsys.readouterr()
    assert "deleted agent" in captured.out and "index failed" in captured.err
