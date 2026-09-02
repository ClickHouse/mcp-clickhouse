"""Static checks that fastmcp.json stays in sync with pyproject.toml's dependencies.

This does not use tomllib (3.11+ only) and does not add a TOML dependency. Instead
it reads the installed (editable) package metadata via importlib.metadata, which
is generated from pyproject.toml at install time, and compares it against the
dependency list declared in fastmcp.json's environment.dependencies.
"""

import json
import re
from importlib.metadata import requires
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FASTMCP_JSON_PATH = REPO_ROOT / "fastmcp.json"
PACKAGE_NAME = "mcp-clickhouse"
IGNORED_PACKAGES = {"fastmcp"}

try:
    from packaging.requirements import Requirement

    def _parse_requirement_name(requirement_string: str) -> str:
        return Requirement(requirement_string).name

except ImportError:  # pragma: no cover - exercised only without packaging installed
    _NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

    def _parse_requirement_name(requirement_string: str) -> str:
        match = _NAME_RE.match(requirement_string)
        if not match:
            raise ValueError(f"Could not parse requirement name from {requirement_string!r}")
        return match.group(1)


def _normalize(name: str) -> str:
    """Normalize a project name for comparison (PEP 503 style)."""
    return name.strip().lower().replace("_", "-")


def _runtime_dependency_names() -> set[str]:
    """Return normalized names of mcp-clickhouse's required (non-extra) dependencies."""
    requirement_strings = requires(PACKAGE_NAME) or []
    names = set()
    for requirement_string in requirement_strings:
        if "extra ==" in requirement_string:
            continue
        name = _parse_requirement_name(requirement_string)
        normalized = _normalize(name)
        if normalized in {_normalize(pkg) for pkg in IGNORED_PACKAGES}:
            continue
        names.add(normalized)
    return names


def _fastmcp_json_dependency_names() -> set[str]:
    with FASTMCP_JSON_PATH.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)
    dependencies = config["environment"]["dependencies"]
    return {_normalize(_parse_requirement_name(dep)) for dep in dependencies}


def test_fastmcp_json_lists_every_runtime_dependency():
    runtime_dependencies = _runtime_dependency_names()
    fastmcp_json_dependencies = _fastmcp_json_dependency_names()

    missing = runtime_dependencies - fastmcp_json_dependencies
    assert not missing, (
        f"fastmcp.json environment.dependencies is missing runtime dependencies: "
        f"{sorted(missing)}. Update {FASTMCP_JSON_PATH} to include them."
    )


def test_fastmcp_json_has_no_stale_dependencies():
    runtime_dependencies = _runtime_dependency_names()
    fastmcp_json_dependencies = _fastmcp_json_dependency_names()

    stale = fastmcp_json_dependencies - runtime_dependencies
    assert not stale, (
        f"fastmcp.json environment.dependencies lists packages that are not runtime "
        f"dependencies of {PACKAGE_NAME} in pyproject.toml: {sorted(stale)}."
    )
