"""JSON encoding for MCP tool results."""

import json
from typing import Any, Iterable, Optional, Sequence

import simplejson


_JS_MAX_SAFE_INTEGER = 9007199254740991


def _stringify_unsafe_integers(
    obj: Any, active_container_ids: Optional[set[int]] = None
) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return str(int(obj)) if abs(obj) > _JS_MAX_SAFE_INTEGER else obj
    if not isinstance(obj, (dict, list, tuple)):
        return obj

    if active_container_ids is None:
        active_container_ids = set()
    container_id = id(obj)
    if container_id in active_container_ids:
        return obj

    active_container_ids.add(container_id)
    try:
        if isinstance(obj, dict):
            items = iter(obj.items())
            for key, value in items:
                converted = _stringify_unsafe_integers(value, active_container_ids)
                if converted is value:
                    continue
                result = dict(obj)
                result[key] = converted
                for remaining_key, remaining_value in items:
                    result[remaining_key] = _stringify_unsafe_integers(
                        remaining_value, active_container_ids
                    )
                return result
            return obj

        items = enumerate(obj)
        for index, value in items:
            converted = _stringify_unsafe_integers(value, active_container_ids)
            if converted is value:
                continue
            result = list(obj)
            result[index] = converted
            for remaining_index, remaining_value in items:
                result[remaining_index] = _stringify_unsafe_integers(
                    remaining_value, active_container_ids
                )
            return result
        return obj
    finally:
        active_container_ids.remove(container_id)


def _serialize_tool_result_with_simplejson(obj: Any) -> str:
    return simplejson.dumps(
        obj,
        default=str,
        bigint_as_string=True,
        allow_nan=True,
        use_decimal=False,
        namedtuple_as_object=False,
        encoding=None,
    )


def _serialize_tool_result_with_stdlib(obj: Any) -> str:
    return json.dumps(_stringify_unsafe_integers(obj), default=str)


# The pure Python simplejson encoder is slower than the stdlib fallback.
if getattr(simplejson.encoder, "c_make_encoder", None) is not None:
    _serialize_tool_result = _serialize_tool_result_with_simplejson
else:
    _serialize_tool_result = _serialize_tool_result_with_stdlib


def _serialize_limited_query_result(
    columns: Sequence[str], rows: Iterable[Any], max_bytes: int
) -> str:
    """Encode complete rows within a JSON byte budget, stopping at the first overflow.

    Complete results keep the existing shape. A truncated result reserves space for
    its marker, dropping previously accepted rows if necessary. Only the encoded
    prefix is retained; the caller owns and closes the database stream.
    """
    prefix = '{"columns": ' + _serialize_tool_result(columns) + ', "rows": ['
    suffix = "]}"
    prefix_size = len(prefix.encode("utf-8"))
    if prefix_size + len(suffix) > max_bytes:
        raise ValueError(
            "Query result columns exceed CLICKHOUSE_MCP_MAX_RESULT_BYTES; "
            "select fewer columns or increase the limit"
        )

    truncated_suffix = "], " + _serialize_tool_result(
        {"truncated": True, "max_result_bytes": max_bytes}
    )[1:]
    truncated_size = len(truncated_suffix.encode("utf-8"))
    encoded_rows = []
    row_sizes = []
    content_size = 0
    for row in rows:
        encoded = _serialize_tool_result(row)
        row_size = len(encoded.encode("utf-8"))
        additional = row_size + (2 if encoded_rows else 0)
        if prefix_size + content_size + additional + len(suffix) > max_bytes:
            while encoded_rows and prefix_size + content_size + truncated_size > max_bytes:
                encoded_rows.pop()
                content_size -= row_sizes.pop() + (2 if encoded_rows else 0)
            if prefix_size + content_size + truncated_size > max_bytes:
                raise ValueError(
                    "CLICKHOUSE_MCP_MAX_RESULT_BYTES is too small for columns and "
                    "the truncation marker; select fewer columns or increase the limit"
                )
            return prefix + ", ".join(encoded_rows) + truncated_suffix
        encoded_rows.append(encoded)
        row_sizes.append(row_size)
        content_size += additional
    return prefix + ", ".join(encoded_rows) + suffix
