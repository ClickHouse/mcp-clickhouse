"""Consistency checks between server.json and the pyproject/README contract.

This branch deliberately does not edit server.json or pyproject.toml (see
MIGRATION_DECISIONS.md D8/D23), so the tests here pin what is true today and
mark the two known gaps xfail(strict=True): a version bump that happens at
release prep, and a documented subset of environment variables that server.json
intentionally limits to the stdio-relevant set. strict=True means either test
starts failing the moment someone fixes the underlying gap, which is the signal
to remove the marker.
"""

import importlib.metadata
import json
import re
from pathlib import Path

import pytest

try:
    from packaging.version import Version

    _HAS_PACKAGING = True
except ImportError:  # pragma: no cover - packaging is a transitive dependency here
    _HAS_PACKAGING = False

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_JSON_PATH = _REPO_ROOT / "server.json"
_README_PATH = _REPO_ROOT / "README.md"
_MCP_ENV_PATH = _REPO_ROOT / "mcp_clickhouse" / "mcp_env.py"

_PEP440_RE = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


def _load_server_json() -> dict:
    return json.loads(_SERVER_JSON_PATH.read_text())


def _pyproject_version() -> str:
    return importlib.metadata.version("mcp-clickhouse")


def _oci_tag(identifier: str) -> str:
    """The version-like tag suffix of an OCI image identifier, e.g. "0.4.0" from
    "ghcr.io/clickhouse/mcp-clickhouse:0.4.0"."""
    _, _, tag = identifier.partition(":")
    assert tag, identifier
    return tag


def _readme_documented_env_vars() -> set[str]:
    """Backticked CLICKHOUSE_*, CHDB_*, and MCP_MIDDLEWARE_MODULE names in the README."""
    text = _README_PATH.read_text()
    return set(
        re.findall(r"`((?:CLICKHOUSE|CHDB)_[A-Z0-9_]*|MCP_MIDDLEWARE_MODULE)`", text)
    )


def _server_json_env_var_names(server: dict) -> list[set[str]]:
    """The environmentVariables name set for each packages[] entry, in order."""
    return [
        {var["name"] for var in pkg["environmentVariables"]} for pkg in server["packages"]
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "server.json is bumped at release prep and is intentionally left at 0.4.0 "
        "while pyproject.toml is 0.5.0 on this branch; see MIGRATION_DECISIONS.md "
        "D8/D23. This starts passing (then XPASS-failing) the moment the versions "
        "are synced, which forces removal of this marker."
    ),
)
def test_server_json_versions_match_pyproject_version():
    server = _load_server_json()
    pyproject_version = _pyproject_version()

    assert server["version"] == pyproject_version
    for pkg in server["packages"]:
        if "version" in pkg:
            assert pkg["version"] == pyproject_version
        if pkg["registryType"] == "oci":
            assert _oci_tag(pkg["identifier"]) == pyproject_version


def test_server_json_and_pyproject_versions_are_valid_pep440():
    server = _load_server_json()
    pyproject_version = _pyproject_version()
    candidates = {server["version"], pyproject_version}
    for pkg in server["packages"]:
        if "version" in pkg:
            candidates.add(pkg["version"])
        if pkg["registryType"] == "oci":
            candidates.add(_oci_tag(pkg["identifier"]))

    for candidate in candidates:
        if _HAS_PACKAGING:
            Version(candidate)  # raises InvalidVersion if not PEP 440
        else:
            assert _PEP440_RE.match(candidate), candidate


def test_server_json_packages_declare_identical_environment_variable_names():
    server = _load_server_json()
    name_sets = _server_json_env_var_names(server)
    assert len(name_sets) >= 2, "expected at least two packages[] entries to compare"
    first, *rest = name_sets
    for other in rest:
        assert other == first


def test_every_server_json_env_var_is_documented_and_read():
    """No-stale-entries direction: every server.json variable must still be real."""
    server = _load_server_json()
    readme_vars = _readme_documented_env_vars()
    mcp_env_source = _MCP_ENV_PATH.read_text()

    all_names = set()
    for pkg in server["packages"]:
        all_names.update(var["name"] for var in pkg["environmentVariables"])

    undocumented = sorted(name for name in all_names if name not in readme_vars)
    assert not undocumented, f"server.json variables missing from README.md: {undocumented}"

    unread = sorted(name for name in all_names if name not in mcp_env_source)
    assert not unread, f"server.json variables not read in mcp_env.py: {unread}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "server.json documents only the stdio-relevant subset of environment "
        "variables; README.md also documents HTTP transport, auth, chDB, and "
        "middleware variables that server.json omits by design. Completing the "
        "list is a release-prep decision, not something this branch changes."
    ),
)
def test_every_readme_documented_env_var_is_in_server_json():
    server = _load_server_json()
    readme_vars = _readme_documented_env_vars()

    all_names = set()
    for pkg in server["packages"]:
        all_names.update(var["name"] for var in pkg["environmentVariables"])

    missing = sorted(readme_vars - all_names)
    assert not missing, f"README-documented variables missing from server.json: {missing}"
