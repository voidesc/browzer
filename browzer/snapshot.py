"""A page as an outline an agent can read and act on: the accessibility tree, one line per
thing that has a name or can be used, each with a ref (`e` + the node's backend id) that
`click` and `fill` take."""

INTERACTIVE = {"link", "button", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox", "option",
               "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider", "spinbutton", "textarea"}
SKIP = {"none", "generic", "InlineTextBox", "LineBreak", "RootWebArea", "paragraph", "group", "presentation",
        "ListMarker", "LayoutTable", "LayoutTableRow", "LayoutTableCell", "LabelText", "DescriptionListDetail", "Section"}
STATES = ("checked", "selected", "expanded", "disabled", "required", "focused")
MAX_LINES = 500
MAX_NAME = 160


def _value(field):
    return field.get("value") if isinstance(field, dict) else None


def render(nodes, max_lines=MAX_LINES):
    """The outline of Accessibility.getFullAXTree's nodes."""
    by_id = {n["nodeId"]: n for n in nodes}
    children = {n["nodeId"]: n.get("childIds", []) for n in nodes}
    roots = [n for n in nodes if "parentId" not in n or n["parentId"] not in by_id] or nodes[:1]
    lines = []

    def walk(node, depth, said):
        role = str(_value(node.get("role")) or "")
        name = " ".join(str(_value(node.get("name")) or "").split())
        shown = not node.get("ignored") and role not in SKIP and (name or role in INTERACTIVE)
        if shown and role == "StaticText" and name in said:
            shown = False   # the text of the link or button above, said once
        if shown:
            line = "  " * depth + ("text" if role == "StaticText" else role)
            if name:
                line += ' "' + (name if len(name) <= MAX_NAME else name[:MAX_NAME - 1] + "…") + '"'
            value = _value(node.get("value"))
            if value not in (None, "") and str(value) != name:
                line += f" value={str(value)[:80]!r}"
            for prop in node.get("properties", []):
                state = _value(prop.get("value"))
                if prop.get("name") in STATES and state not in (False, "false", None):
                    line += f" {prop['name']}" if state in (True, "true") else f" {prop['name']}={state}"
            if role != "StaticText" and "backendDOMNodeId" in node:
                line += f" [e{node['backendDOMNodeId']}]"
            lines.append(line)
            depth, said = depth + 1, name or said
        for child in children.get(node["nodeId"], []):
            if child in by_id:
                walk(by_id[child], depth, said)

    for root in roots:
        walk(root, 0, "")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… {len(lines) - max_lines} more lines"]
    return "\n".join(lines)
