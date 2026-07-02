"""Unit tests for the self-contained chDB output-bounding helper.

Pure-function tests with no chDB or server dependency. SQL source scanning for
the file allowlist is owned (and tested) upstream in ``chdb.agents.safety``;
its integration into the query gate is covered by test_chdb_security_baseline.
"""

from mcp_clickhouse.chdb_safety import truncate_text


def test_truncate_text_passthrough_when_within_budget():
    assert truncate_text("short", 100) == "short"


def test_truncate_text_cuts_and_appends_notice():
    out = truncate_text("x" * 100, 10)
    head = out.split("\n\n")[0]
    assert head == "x" * 10
    assert "truncated at 10 bytes" in out


def test_truncate_text_respects_byte_budget_for_multibyte():
    # 10 two-byte chars = 20 bytes; budget 5 bytes -> at most 5 bytes kept
    out = truncate_text("é" * 10, 5)
    head = out.split("\n\n")[0]
    assert len(head.encode("utf-8")) <= 5
