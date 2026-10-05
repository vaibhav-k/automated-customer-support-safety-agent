"""
Fail if secrets or secret-bearing files are tracked by git (used by CI and runnable locally).

Checks every file in ``git ls-files``:

* **Forbidden files**: ``.env``, ``azvars.ps1``, ``src/local.settings.json`` and similar must never be committed.
* **Secret-looking values**: an ``*key``/``*secret``/``*password``/``x-functions-key`` setting assigned a long
  literal, Azure storage ``AccountKey=``, or connection strings with ``SharedAccessKey=``.

Ignored: values that are clearly placeholders (``<...>``, ``$env:...``, ``REDACTED``, ``xxxx``), values without a
digit (code references such as ``settings.search_api_key``), and lines marked ``secret-scan: ignore`` (test fixtures).

Usage::

    python -m scripts.check_secrets            # exit 1 and list findings if anything is found
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_FILES = re.compile(
    r"(^|/)(\.env|azvars\.ps1|[^/]*\.azvars\.ps1|local\.settings\.json|[^/]*\.publishsettings)$"
)

SECRET_PATTERNS = {
    "key/secret assignment": re.compile(
        r"(?i)\b[\w.-]*(api[_-]?key|secret|password|x-functions-key|subscription[_-]?key)\b[\"']?\s*[:=]\s*"
        r"[\"']?(?P<value>[A-Za-z0-9_+/=.-]{20,})"
    ),
    "storage account key": re.compile(r"(?i)AccountKey=(?P<value>[A-Za-z0-9+/=]{20,})"),
    "shared access key": re.compile(r"(?i)SharedAccessKey=(?P<value>[A-Za-z0-9+/=]{20,})"),
}
PLACEHOLDER = re.compile(r"(?i)(^<|^\$|redacted|x{4,}|example|placeholder|your[_-]|dummy|fake)")
IGNORE_MARKER = "secret-scan: ignore"
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".whl"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}" if self.line else f"{self.path}: {self.kind}"


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line]


def scan_text(path: str, text: str) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if IGNORE_MARKER in line:
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            match = pattern.search(line)
            if match and _looks_secret(match.group("value")):
                findings.append(Finding(path, number, kind))
    return findings


def _looks_secret(value: str) -> bool:
    return any(ch.isdigit() for ch in value) and not PLACEHOLDER.search(value)


def scan(paths: list[str]) -> list[Finding]:
    findings = [Finding(p, 0, "file must not be committed") for p in paths if FORBIDDEN_FILES.search(p)]
    for path in paths:
        file = REPO_ROOT / path
        if file.suffix.lower() in BINARY_SUFFIXES or not file.is_file():
            continue
        findings.extend(scan_text(path, file.read_text(encoding="utf-8", errors="ignore")))
    return findings


def main() -> int:
    try:
        paths = tracked_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"Could not list tracked files (is this a git repo?): {exc}",
            file=sys.stderr,
        )
        return 2
    findings = scan(paths)
    for finding in findings:
        print(f"::error file={finding.path},line={max(finding.line, 1)}::{finding.kind}")
    print(f"Scanned {len(paths)} tracked files: {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
