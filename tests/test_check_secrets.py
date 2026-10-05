"""Tests for scripts/check_secrets.py."""

import pytest

from scripts.check_secrets import scan, scan_text


@pytest.mark.parametrize(
    "line",
    [
        "AZURE_SEARCH_API_KEY=Abc123def456GHI789jkl012mno345",  # secret-scan: ignore
        'x-functions-key: "wBaJfbOBpYWloDaWIRXq0aBc12345678"',  # secret-scan: ignore
        "AccountName=a;AccountKey=QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=",  # secret-scan: ignore
        "$secret = 'p4ssw0rd-that-is-long-enough-1234'",  # secret-scan: ignore
    ],
)
def test_real_looking_secrets_are_found(line):
    assert scan_text("f.txt", line)


@pytest.mark.parametrize(
    "line",
    [
        "AZURE_SEARCH_API_KEY=",
        "CONTENT_SAFETY_API_KEY=<key 1>",
        "$KEY = az functionapp function keys list -g $RG -n $FUNCAPP --query default -o tsv",
        "api_key=settings.search_api_key",
        'headers={"x-functions-key": "REDACTED"}',
        'api_key="lab-key"',
    ],
)
def test_placeholders_and_code_are_ignored(line):
    assert scan_text("f.txt", line) == []


def test_ignore_marker_skips_fixture_lines():
    assert scan_text("t.py", "KEY=Abc123def456GHI789jkl012mno345  # secret-scan: ignore") == []


def test_forbidden_files_are_flagged():
    findings = scan([".env", "azvars.ps1", "src/local.settings.json", ".env.example", "README.md"])
    flagged = {f.path for f in findings if f.kind == "file must not be committed"}
    assert flagged == {".env", "azvars.ps1", "src/local.settings.json"}
