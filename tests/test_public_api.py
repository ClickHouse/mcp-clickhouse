"""The mcp_clickhouse package namespace: supported API and deprecated internals.

MIGRATION_DECISIONS.md D21: the synchronous helpers and create_clickhouse_client
are the supported Python API. The four pagination internals stay importable from
the package for one release with a DeprecationWarning and leave __all__ in the
next minor release.
"""

import warnings

import pytest

import mcp_clickhouse
from mcp_clickhouse import mcp_server

DEPRECATED_INTERNALS = (
    "table_pagination_cache",
    "fetch_table_names_from_system",
    "get_paginated_table_data",
    "create_page_token",
)
SUPPORTED_API = (
    "list_databases",
    "list_tables",
    "run_query",
    "create_clickhouse_client",
    "create_chdb_client",
    "run_chdb_select_query",
    "chdb_initial_prompt",
)


@pytest.mark.parametrize("name", DEPRECATED_INTERNALS)
def test_deprecated_internal_warns_and_resolves_to_the_server_object(name: str):
    with pytest.warns(DeprecationWarning, match=f"mcp_clickhouse.{name} is an internal"):
        value = getattr(mcp_clickhouse, name)

    assert value is getattr(mcp_server, name)


@pytest.mark.parametrize("name", SUPPORTED_API)
def test_supported_api_does_not_warn(name: str):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        value = getattr(mcp_clickhouse, name)

    assert value is getattr(mcp_server, name)


def test_deprecated_internals_are_still_listed_in_all_this_release():
    """Removing them from __all__ is the next-minor step; make it a deliberate edit."""
    assert set(DEPRECATED_INTERNALS) <= set(mcp_clickhouse.__all__)
    assert set(SUPPORTED_API) <= set(mcp_clickhouse.__all__)


def test_deprecated_internals_are_listed_by_dir_without_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        listed = dir(mcp_clickhouse)

    assert set(DEPRECATED_INTERNALS) <= set(listed)
    assert set(SUPPORTED_API) <= set(listed)


def test_unknown_attribute_still_raises_attribute_error():
    with pytest.raises(AttributeError, match="no attribute 'definitely_missing'"):
        mcp_clickhouse.definitely_missing
