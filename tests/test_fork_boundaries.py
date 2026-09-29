"""Fork-boundary guards for Amadeus.

These tests encode the layering rules documented in ``AGENTS.md``:

- ``tau_ai`` and ``tau_agent`` are the reusable Tau core and must not depend on
  ``tau_coding`` or ``tau_incident``.
- ``tau_incident`` is the incident domain package and must not depend on
  ``tau_coding``; application wiring lives in ``tau_coding.incident``.

The import checks are static (AST based) so they are fast and never import the
modules under test.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import tau_coding.update_check as update_check
import tau_coding.version as version_module

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CORE_PACKAGES = ("tau_ai", "tau_agent")
DOMAIN_PACKAGE = "tau_incident"
APPLICATION_PACKAGES = ("tau_coding", "tau_incident")


def _imported_roots(path: Path) -> set[str]:
    """Return the top-level module names imported by a Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _python_files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def test_core_packages_do_not_import_application_packages() -> None:
    forbidden = set(APPLICATION_PACKAGES)
    offenders: list[str] = []
    for package in CORE_PACKAGES:
        for path in _python_files(package):
            leaked = _imported_roots(path) & forbidden
            if leaked:
                offenders.append(f"{path.relative_to(ROOT)}: {sorted(leaked)}")
    assert offenders == [], (
        "Tau core packages must stay independent of Amadeus application/domain "
        f"packages: {offenders}"
    )


def test_incident_domain_does_not_import_coding_app() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_files(DOMAIN_PACKAGE)
        if "tau_coding" in _imported_roots(path)
    ]
    assert offenders == [], (
        "tau_incident must not import tau_coding; wire the application in "
        f"tau_coding/incident instead: {offenders}"
    )


def test_distribution_name_is_consistent() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    distribution_name = pyproject["project"]["name"]

    assert distribution_name == "amadeus"
    assert distribution_name == version_module._DISTRIBUTION_NAME
    assert distribution_name == update_check.PYPI_PACKAGE_NAME


def test_cli_command_stays_tau() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"] == {"tau": "tau_coding.cli:app"}
