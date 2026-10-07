"""Small, dependency-free projection for direct-device UI observations."""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree


DEFAULT_MAX_NODES = 200
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_ROLES = {
    "Button": "button",
    "CheckBox": "checkbox",
    "EditText": "text_input",
    "ImageButton": "button",
    "ImageView": "image",
    "ListView": "list",
    "RadioButton": "radio_button",
    "RecyclerView": "list",
    "ScrollView": "scroll_view",
    "SeekBar": "slider",
    "Switch": "switch",
    "TextView": "text",
    "WebView": "web_view",
}


class UiTreeProjectionError(ValueError):
    """The raw XML cannot safely be represented as a compact UI tree."""


def compact_ui_tree(content: bytes, *, max_nodes: int = DEFAULT_MAX_NODES) -> dict[str, Any]:
    """Project useful UI nodes while retaining actionable empty controls.

    Empty layout containers are omitted.  ``parent_id`` refers to the nearest
    retained node, preserving useful navigation context without recreating the
    complete layout tree for every returned node.
    """

    if max_nodes < 1:
        raise ValueError("max_nodes must be positive")
    try:
        root = ElementTree.fromstring(content)
    except (TypeError, ValueError, ElementTree.ParseError) as exc:
        raise UiTreeProjectionError("ui_tree_xml_invalid") from exc

    candidates: list[dict[str, Any]] = []

    def visit(element: ElementTree.Element, path: tuple[int, ...], parent_id: str | None) -> None:
        attributes = element.attrib
        text = _text(attributes.get("text"))
        description = _text(attributes.get("content-desc"))
        resource_id = _text(attributes.get("resource-id"))
        clickable = _optional_boolean(attributes.get("clickable"))
        scrollable = _optional_boolean(attributes.get("scrollable"))
        checkable = _optional_boolean(attributes.get("checkable"))
        checked = _optional_boolean(attributes.get("checked"))
        enabled = _optional_boolean(attributes.get("enabled"))
        focusable = _optional_boolean(attributes.get("focusable"))
        long_clickable = _optional_boolean(attributes.get("long-clickable"))
        retained = bool(
            text or description or resource_id
            or clickable is True or scrollable is True or checkable is True
            or focusable is True or long_clickable is True
        )
        next_parent_id = parent_id
        if retained:
            node_id = ".".join(str(item) for item in path)
            class_name = _text(attributes.get("class"))
            node: dict[str, Any] = {
                "id": node_id,
                "parent_id": parent_id,
                "bounds": _bounds(attributes.get("bounds")),
                "clickable": clickable,
                "scrollable": scrollable,
                "checkable": checkable,
                "checked": checked,
                "enabled": enabled,
                "focusable": focusable,
                "long_clickable": long_clickable,
            }
            if text:
                node["text"] = text
            if description:
                node["description"] = description
            if resource_id:
                node["resource_id"] = resource_id
            if class_name:
                node["class"] = class_name
                role = _role(class_name)
                if role:
                    node["role"] = role
            candidates.append(node)
            next_parent_id = node_id
        for index, child in enumerate(element):
            visit(child, (*path, index), next_parent_id)

    visit(root, (0,), None)
    returned = candidates[:max_nodes]
    return {"nodes": returned, "total": len(candidates), "returned": len(returned), "truncated": len(candidates) > len(returned)}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _optional_boolean(value: object) -> bool | None:
    if not isinstance(value, str):
        return None
    return value.casefold() == "true"


def _bounds(value: object) -> list[int] | None:
    if not isinstance(value, str):
        return None
    match = _BOUNDS.fullmatch(value)
    if match is None:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    if left > right or top > bottom:
        return None
    return [left, top, right, bottom]


def _role(class_name: str) -> str | None:
    suffix = class_name.rsplit(".", 1)[-1]
    return _ROLES.get(suffix)
