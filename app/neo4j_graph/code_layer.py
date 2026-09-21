"""Build the code-structure layer via tree-sitter (language-agnostic).

For every tracked source file we extract Class / Interface / Function /
Parameter nodes plus DEFINED_IN, IMPORTS, INHERITS, CALLS, HAS_PARAMETER edges.
Languages without a spec still keep their File node (from the git layer); they
just contribute no symbols.

CALLS / INHERITS are resolved within a repo by simple name: a call resolves to
a Function when the name is unique (same-file first, then repo-wide); an
unresolved base class becomes an ExternalClass so the edge is never lost.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from tree_sitter_language_pack import get_parser

from .config import GraphBuildConfig, repo_local_path
from .writer import Neo4jWriter

log = logging.getLogger(__name__)

LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".php": "php", ".phtml": "php",
    ".java": "java",
    ".go": "go",
}

_NAME_NODES = {
    "identifier", "property_identifier", "type_identifier", "name",
    "field_identifier", "word", "variable_name", "constant",
    "scoped_identifier", "namespaced_name", "qualified_identifier",
}

# Per-language node-type vocabulary.
_SPECS: dict[str, dict[str, set[str]]] = {
    "python": {
        "class": {"class_definition"},
        "interface": set(),
        "func": {"function_definition"},
        "import": {"import_statement", "import_from_statement"},
        "call": {"call"},
        "params": {"parameters"},
        "param_node": {"identifier", "typed_parameter", "default_parameter",
                       "typed_default_parameter", "list_splat_pattern",
                       "dictionary_splat_pattern"},
    },
    "javascript": {
        "class": {"class_declaration", "class"},
        "interface": set(),
        "func": {"function_declaration", "generator_function_declaration",
                 "method_definition"},
        "import": {"import_statement"},
        "call": {"call_expression"},
        "params": {"formal_parameters"},
        "param_node": {"identifier", "required_parameter", "optional_parameter",
                       "rest_pattern", "assignment_pattern", "object_pattern",
                       "array_pattern"},
    },
    "go": {
        "class": {"type_spec"},
        "interface": set(),
        "func": {"function_declaration", "method_declaration"},
        "import": {"import_declaration"},
        "call": {"call_expression"},
        "params": {"parameter_list"},
        "param_node": {"parameter_declaration"},
    },
    "java": {
        "class": {"class_declaration", "enum_declaration", "record_declaration"},
        "interface": {"interface_declaration"},
        "func": {"method_declaration", "constructor_declaration"},
        "import": {"import_declaration"},
        "call": {"method_invocation", "object_creation_expression"},
        "params": {"formal_parameters"},
        "param_node": {"formal_parameter", "spread_parameter"},
    },
    "php": {
        "class": {"class_declaration", "trait_declaration", "enum_declaration"},
        "interface": {"interface_declaration"},
        "func": {"function_definition", "method_declaration"},
        "import": {"namespace_use_declaration"},
        "call": {"function_call_expression", "member_call_expression",
                 "scoped_call_expression", "object_creation_expression"},
        "params": {"formal_parameters"},
        "param_node": {"simple_parameter", "variadic_parameter",
                       "property_promotion_parameter"},
    },
}
# TS/TSX reuse the JS vocabulary plus interfaces + abstract classes.
_SPECS["typescript"] = {
    **_SPECS["javascript"],
    "class": _SPECS["javascript"]["class"] | {"abstract_class_declaration"},
    "interface": {"interface_declaration"},
}
_SPECS["tsx"] = _SPECS["typescript"]


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node.text else ""


def _last_name(node) -> str | None:
    """Rightmost identifier-ish leaf in a subtree (callee / base name)."""
    found = None
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in _NAME_NODES and n.child_count == 0:
            found = n  # last in DFS pre-order with reversed push == rightmost
        stack.extend(reversed(n.children))
    return _text(found) if found else None


def _node_name(node) -> str | None:
    n = node.child_by_field_name("name")
    if n is not None:
        return _text(n).lstrip("$")
    for c in node.children:
        if c.type in _NAME_NODES:
            return _text(c).lstrip("$")
    return None


def _param_names(func_node, spec) -> list[str]:
    container = None
    for c in func_node.children:
        if c.type in spec["params"]:
            container = c
            break
    if container is None:
        return []
    names: list[str] = []
    for child in container.children:
        if child.type not in spec["param_node"] and child.type not in _NAME_NODES:
            continue
        nm = _node_name(child) or _last_name(child)
        if nm and nm not in ("self", "this"):
            names.append(nm)
    return names


def _import_modules(node, lang) -> list[str]:
    """Pull module/namespace strings out of an import statement."""
    mods: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("string", "string_literal", "interpreted_string_literal"):
            mods.append(_text(n).strip("\"'`"))
        elif lang in ("php", "java", "go") and n.type in (
            "qualified_name", "namespace_name", "scoped_identifier",
            "namespaced_name", "package_identifier",
        ):
            mods.append(_text(n))
        stack.extend(n.children)
    # de-dup, keep order
    seen, out = set(), []
    for m in mods:
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out[:1] if lang in ("python", "javascript", "typescript", "tsx") and out else out


_TYPE_NAME_NODES = {
    "type_identifier", "identifier", "name", "constant",
    "scoped_type_identifier", "qualified_name", "namespaced_name",
    "scoped_identifier", "qualified_type",
}
_HERITAGE_KEYWORDS = {"extends", "implements", ",", ":"}


def _type_names_in(node) -> list[str]:
    """Type names referenced in a heritage subtree, skipping generic args."""
    out: list[str] = []
    queue = [node]
    while queue:
        n = queue.pop(0)
        if n.type in ("type_arguments", "type_parameters"):
            continue  # ignore generic parameters (extends Foo<Bar>)
        if n.type in _TYPE_NAME_NODES:
            txt = _text(n).strip()
            if txt and txt not in _HERITAGE_KEYWORDS:
                out.append(txt)
            continue  # treat as leaf even if it has token children
        queue.extend(n.children)
    return out


def _base_names(class_node, lang) -> list[str]:
    bases: list[str] = []

    if lang == "python":
        sup = class_node.child_by_field_name("superclasses")
        if sup:
            bases += _type_names_in(sup)
    elif lang in ("javascript", "typescript", "tsx"):
        for c in class_node.children:
            if c.type in ("class_heritage", "extends_clause", "implements_clause"):
                bases += _type_names_in(c)
    elif lang == "java":
        sup = class_node.child_by_field_name("superclass")
        if sup:
            bases += _type_names_in(sup)
        for c in class_node.children:
            if c.type in ("super_interfaces", "extends_interfaces"):
                bases += _type_names_in(c)
    elif lang == "php":
        for c in class_node.children:
            if c.type in ("base_clause", "class_interface_clause"):
                bases += _type_names_in(c)
    return [b for b in dict.fromkeys(bases) if b]


def _call_name(node) -> str | None:
    fn = node.child_by_field_name("function") or node.child_by_field_name("name")
    return _last_name(fn) if fn is not None else _last_name(node)


class _FileParse:
    """Symbols + references extracted from a single file."""

    def __init__(self, repo: str, relpath: str):
        self.repo = repo
        self.relpath = relpath
        self.classes: list[dict] = []      # {uid,name,start,end,label}
        self.functions: list[dict] = []    # {uid,name,start,end,enclosing_class}
        self.params: list[tuple[str, str]] = []   # (func_uid, pname)
        self.imports: list[str] = []
        self.inherits: list[tuple[str, str]] = []  # (class_uid, base_name)
        self.calls: list[tuple[str, str]] = []      # (func_uid, callee_name)

    def _uid(self, kind: str, name: str, line: int) -> str:
        return f"{self.repo}:{self.relpath}#{kind}:{name}:{line}"


def parse_file(repo: str, relpath: str, src: bytes, lang: str) -> _FileParse:
    spec = _SPECS[lang]
    parser = get_parser(lang)
    tree = parser.parse(src)
    fp = _FileParse(repo, relpath)

    # track nearest enclosing function for CALLS source via a manual stack
    _current_func: list[str] = []

    def walk2(node, enclosing_class):
        ntype = node.type
        if ntype in spec["import"]:
            fp.imports.extend(_import_modules(node, lang))
            return
        is_class = ntype in spec["class"] or ntype in spec["interface"]
        is_func = ntype in spec["func"]
        if is_class:
            name = _node_name(node)
            if name:
                label = "Interface" if ntype in spec["interface"] else "Class"
                uid = fp._uid("C", name, node.start_point[0] + 1)
                fp.classes.append({
                    "uid": uid, "name": name, "label": label,
                    "start": node.start_point[0] + 1, "end": node.end_point[0] + 1,
                })
                for base in _base_names(node, lang):
                    fp.inherits.append((uid, base))
                for c in node.children:
                    walk2(c, uid)
                return
        if is_func:
            name = _node_name(node)
            if name:
                uid = fp._uid("F", name, node.start_point[0] + 1)
                fp.functions.append({
                    "uid": uid, "name": name,
                    "start": node.start_point[0] + 1, "end": node.end_point[0] + 1,
                    "enclosing_class": enclosing_class,
                })
                for pname in _param_names(node, spec):
                    fp.params.append((uid, pname))
                _current_func.append(uid)
                for c in node.children:
                    walk2(c, enclosing_class)
                _current_func.pop()
                return
        if ntype in spec["call"]:
            callee = _call_name(node)
            if callee and _current_func:
                fp.calls.append((_current_func[-1], callee))
        for c in node.children:
            walk2(c, enclosing_class)

    walk2(tree.root_node, None)
    return fp


def build_code_layer(
    writer: Neo4jWriter, repo: dict[str, Any], tracked: list[str], cfg: GraphBuildConfig
) -> dict[str, int]:
    name = repo["name"]
    repo_path = Path(repo_local_path(repo))
    parses: list[_FileParse] = []
    lang_counts: dict[str, int] = defaultdict(int)

    for rel in tracked:
        lang = LANG_BY_EXT.get(PurePosixPath(rel).suffix.lower())
        if not lang:
            continue
        abspath = repo_path / rel
        try:
            if abspath.stat().st_size > cfg.max_file_bytes:
                continue
            src = abspath.read_bytes()
        except OSError:
            continue
        try:
            fp = parse_file(name, rel, src, lang)
        except Exception as exc:  # never let one bad file kill the repo
            log.debug("[%s] parse failed for %s: %s", name, rel, exc)
            continue
        parses.append(fp)
        lang_counts[lang] += 1

    # ── repo-wide symbol tables for resolution ───────────────────────
    func_by_name: dict[str, list[str]] = defaultdict(list)
    type_by_name: dict[str, str] = {}      # name -> uid (first wins)
    for fp in parses:
        for f in fp.functions:
            func_by_name[f["name"]].append(f["uid"])
        for c in fp.classes:
            type_by_name.setdefault(c["name"], c["uid"])

    class_nodes, iface_nodes, func_nodes, param_nodes = [], [], [], []
    module_names: set[str] = set()
    external_classes: set[str] = set()
    defined_in_file, method_in_class = [], []
    imports_rel, inherits_cls, inherits_ext = [], [], []
    has_param, calls_rel = [], []

    for fp in parses:
        file_uid = f"{name}:{fp.relpath}"
        funcs_here = {f["name"] for f in fp.functions}

        for c in fp.classes:
            node = {"uid": c["uid"], "repo": name, "file": fp.relpath,
                    "name": c["name"], "start_line": c["start"], "end_line": c["end"]}
            if c["label"] == "Interface":
                iface_nodes.append(node)
                defined_in_file.append({"start": c["uid"], "end": file_uid, "_label": "Interface"})
            else:
                class_nodes.append(node)
                defined_in_file.append({"start": c["uid"], "end": file_uid, "_label": "Class"})

        for f in fp.functions:
            func_nodes.append({
                "uid": f["uid"], "repo": name, "file": fp.relpath, "name": f["name"],
                "start_line": f["start"], "end_line": f["end"],
                "is_method": bool(f["enclosing_class"]),
            })
            if f["enclosing_class"]:
                method_in_class.append({"start": f["uid"], "end": f["enclosing_class"]})
            else:
                defined_in_file.append({"start": f["uid"], "end": file_uid, "_label": "Function"})

        for func_uid, pname in fp.params:
            puid = f"{func_uid}::{pname}"
            param_nodes.append({"uid": puid, "name": pname})
            has_param.append({"start": func_uid, "end": puid})

        for mod in fp.imports:
            module_names.add(mod)
            imports_rel.append({"start": file_uid, "end": mod})

        for class_uid, base in fp.inherits:
            target = type_by_name.get(base)
            if target and target != class_uid:
                inherits_cls.append({"start": class_uid, "end": target})
            else:
                external_classes.add(base)
                inherits_ext.append({"start": class_uid, "end": base})

        for func_uid, callee in fp.calls:
            # resolve: same-file unique -> repo-wide unique
            if callee in funcs_here:
                same = [f["uid"] for f in fp.functions if f["name"] == callee]
                if len(same) == 1 and same[0] != func_uid:
                    calls_rel.append({"start": func_uid, "end": same[0]})
                    continue
            candidates = func_by_name.get(callee, [])
            if len(candidates) == 1 and candidates[0] != func_uid:
                calls_rel.append({"start": func_uid, "end": candidates[0]})

    # ── write nodes then edges ───────────────────────────────────────
    writer.write_nodes("Class", class_nodes)
    writer.write_nodes("Interface", iface_nodes)
    writer.write_nodes("Function", func_nodes)
    writer.write_nodes("Parameter", param_nodes)
    writer.write_nodes("Module", [{"name": m} for m in module_names])
    writer.write_nodes("ExternalClass", [{"name": c} for c in external_classes])

    for label in ("Class", "Interface", "Function"):
        rows = [{"start": r["start"], "end": r["end"]}
                for r in defined_in_file if r["_label"] == label]
        writer.write_rels("DEFINED_IN", label, "File", rows)
    writer.write_rels("DEFINED_IN", "Function", "Class", method_in_class)
    writer.write_rels("IMPORTS", "File", "Module", imports_rel)
    writer.write_rels("INHERITS", "Class", "Class", inherits_cls)
    writer.write_rels("INHERITS", "Class", "ExternalClass", inherits_ext)
    writer.write_rels("HAS_PARAMETER", "Function", "Parameter", has_param)
    writer.write_rels("CALLS", "Function", "Function", calls_rel)

    log.info("[%s] code: %d classes, %d interfaces, %d functions, %d calls (langs: %s)",
             name, len(class_nodes), len(iface_nodes), len(func_nodes), len(calls_rel),
             dict(lang_counts))
    return {
        "classes": len(class_nodes), "interfaces": len(iface_nodes),
        "functions": len(func_nodes), "calls": len(calls_rel),
        "files_parsed": len(parses),
    }
