"""Self-contained output-bounding helper for the chDB query path.

A pure function with no third-party or project dependencies, so it stays
importable when the optional ``chdb`` extra is not installed. SQL source
scanning for the file allowlist (table-function detection, path checks) ships
in ``chdb.agents.safety`` and is imported lazily on the chDB-only code paths.

Identifier/string quoting for the introspection tools is NOT here — those tools
go through ``chdb.agents.ChDBTool``, which owns quoting and parameter binding.
"""

from __future__ import annotations

_TRUNCATION_NOTICE = (
    "\n\n[... output truncated at {limit} bytes; narrow the query or raise "
    "CHDB_MAX_RESULT_BYTES ...]"
)


def truncate_text(text: str, limit_bytes: int) -> str:
    """Trim text to at most ``limit_bytes`` UTF-8 bytes, appending a notice if cut.

    Trimming is done on the encoded bytes (not characters) so the byte budget is
    respected exactly; a partial trailing multi-byte character is dropped.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    trimmed = encoded[:limit_bytes].decode("utf-8", errors="ignore")
    return trimmed + _TRUNCATION_NOTICE.format(limit=limit_bytes)
