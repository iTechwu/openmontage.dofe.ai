"""Flatten tool schemas before they are advertised to model clients.

GLM and DeepSeek reject a schema that still points at ``#/$defs/...`` with
``Pointer '/$defs/...' does not exist``. That rejection fails the whole
request before the model runs. Local pointers are replaced by the schemas
they name. Argument validation stays on the Pydantic models.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_MAPS = frozenset({"properties", "patternProperties", "dependentSchemas"})


def project_tool_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with local ``#/...`` pointers inlined and definition tables omitted."""
    return _inline_object(dict(parameters), parameters, set())


def publish_flat_tool_schemas(server: Any) -> None:
    """Rewrite each advertised tool schema. Pydantic argument validation is unchanged."""
    for tool in server._tool_manager.list_tools():
        parameters = getattr(tool, "parameters", None)
        if isinstance(parameters, Mapping):
            tool.parameters = project_tool_parameters(parameters)


def _is_record(value: Any) -> bool:
    return isinstance(value, Mapping) and not isinstance(value, Sequence)


def _decode_pointer(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_pointer(root: Any, ref: str) -> Any:
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        return None
    node = root
    for raw in ref[2:].split("/"):
        token = _decode_pointer(raw)
        if isinstance(node, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                return None
            index = int(token)
            if index >= len(node):
                return None
            node = node[index]
            continue
        if not _is_record(node) or token not in node:
            return None
        node = node[token]
    return node


def _inline_map(value: Any, root: Any, stack: set[str]) -> Any:
    if not _is_record(value):
        return _inline(value, root, stack)
    return {name: _inline(schema, root, stack) for name, schema in value.items()}


def _inline(node: Any, root: Any, stack: set[str]) -> Any:
    if isinstance(node, list):
        return [_inline(item, root, stack) for item in node]
    if not _is_record(node):
        return node
    return _inline_object(node, root, stack)


def _inline_object(node: Mapping[str, Any], root: Any, stack: set[str]) -> dict[str, Any]:
    if "$ref" in node:
        return _expand_ref(node, root, stack)
    copy: dict[str, Any] = {}
    for key, value in node.items():
        if key in {"$defs", "definitions", "discriminator"}:
            continue
        if key in SCHEMA_MAPS:
            copy[key] = _inline_map(value, root, stack)
            continue
        copy[key] = _inline(value, root, stack)
    return copy


def _expand_ref(node: Mapping[str, Any], root: Any, stack: set[str]) -> dict[str, Any]:
    ref = node.get("$ref")
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    if not isinstance(ref, str) or ref in stack:
        return _inline_object(siblings, root, stack)
    target = _resolve_pointer(root, ref)
    if target is None:
        return _inline_object(siblings, root, stack)
    stack.add(ref)
    resolved = _inline(target, root, stack)
    stack.remove(ref)
    if not _is_record(resolved):
        return _inline_object(siblings, root, stack)
    return {**resolved, **_inline_object(siblings, root, stack)}
