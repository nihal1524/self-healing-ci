"""
B. Context Extraction (File Reader)

Parses pytest/npm test output to figure out which source file(s) are
implicated in the failure, then reads their full content from the LOCAL
repo path (not from inside the container) so it can be handed to the LLM
as context.

This uses lightweight regexes rather than a full test-report parser to
keep Phase 1 dependency-free. It's intentionally permissive: it's fine to
over-collect a few extra files, the LLM will sort out what's relevant.

On top of the file list, a stdlib-only AST pass pins each failure location
(file:line) to its enclosing function/class and, for failing tests, follows
called ``from module import symbol`` names (and ``obj.method`` uses, by name)
to their local definitions. These
come back as ``focus`` snippets next to the full file content.
"""

import ast
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# pytest short summary line: "FAILED tests/test_foo.py::test_bar - AssertionError"
PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+?)(?:::\S+)?\s", re.MULTILINE)

# Python traceback frames: File "/app/src/foo.py", line 12, in bar
TRACEBACK_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')

# Node/Jest style: "at Object.<anonymous> (/app/src/foo.js:12:5)"
JS_STACK_RE = re.compile(r"\(?(/app/[^\s:()]+\.[jt]sx?):(\d+):(\d+)\)?")

# pytest location lines: "tax.py:6: ZeroDivisionError" / "/app/tests/test_x.py:5: in test_x"
FILE_LINE_RE = re.compile(r"(?P<path>(?:/app/)?[A-Za-z0-9_./\\-]+\.py):(?P<line>\d+)")

# AST nodes that can enclose a failure line
CALLABLES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _strip_app_prefix(path: str) -> str:
    if path.startswith("/app/"):
        return path[len("/app/") :]
    return path.lstrip("/")


def _find_failing_files_from_text(test_output: str) -> set[str]:
    files = set()

    for m in PYTEST_FAILED_RE.finditer(test_output):
        files.add(_strip_app_prefix(m.group(1)))

    for m in TRACEBACK_FILE_RE.finditer(test_output):
        path = _strip_app_prefix(m.group(1))
        # Skip stdlib / site-packages frames, we only want project files
        if "site-packages" not in path and not path.startswith("/"):
            files.add(path)

    for m in JS_STACK_RE.finditer(test_output):
        files.add(_strip_app_prefix(m.group(1)))

    return files


def _find_failing_files_from_junit_xml(xml_text: str) -> set[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return set()

    fragments: list[str] = []

    def walk(node):
        if node.tag in {"failure", "error"}:
            text = node.text or ""
            for child in list(node):
                text += child.tail or ""
            if text.strip():
                fragments.append(text)
        for child in list(node):
            walk(child)

    walk(root)

    files = set()
    for fragment in fragments:
        files |= _find_failing_files_from_text(fragment)

        # JUnit XML often includes file references like "tax.py:6" or
        # "tests/test_calculator.py:5" in failure text, not as a full Python
        # traceback frame string.
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z0-9_./\\-]+\.py):(\d+)", fragment):
            files.add(_strip_app_prefix(match.group(1)))

    for node in root.iter("testcase"):
        classname = node.get("classname")
        if classname:
            # tests.test_calculator -> tests/test_calculator.py
            module_path = classname.replace(".", "/") + ".py"
            files.add(_strip_app_prefix(module_path))

    # Also cover junit attributes when present, e.g. <failure file="src/foo.py" ...>
    for node in root.iter():
        if node.tag in {"failure", "error", "testcase"}:
            candidate = node.get("file")
            if candidate:
                files.add(_strip_app_prefix(candidate))
    return files


def find_failing_files(test_output: str) -> set[str]:
    """Prefer structured JUnit XML failure data when available, otherwise
    fall back to regex parsing of raw pytest or JS output text."""
    files = set()
    if "<testsuite" in test_output or "<?xml" in test_output:
        files |= _find_failing_files_from_junit_xml(test_output)
    files |= _find_failing_files_from_text(test_output)
    return files


# from foo import bar / from foo.bar import baz / import foo
PY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([a-zA-Z_][\w\.]*)", re.MULTILINE)


def _local_imports(file_content: str, repo_path: str) -> set[str]:
    """Resolve absolute Python imports to matching local modules/packages.

    For ``package.module``, include ``package/module.py`` or
    ``package/module/__init__.py`` when present, plus parent package
    ``__init__.py`` files. Relative and external imports are ignored.
    """
    found = set()
    repo_root = os.path.realpath(repo_path)

    def add_if_local(candidate: str) -> None:
        resolved = os.path.realpath(candidate)
        try:
            if os.path.commonpath([repo_root, resolved]) != repo_root:
                return
        except ValueError:
            return
        if os.path.isfile(candidate):
            relative = os.path.relpath(candidate, repo_path)
            found.add(relative.replace(os.sep, "/"))

    for m in PY_IMPORT_RE.finditer(file_content):
        components = m.group(1).split(".")
        module_path = os.path.join(repo_path, *components)

        # Candidate 1: a dotted import may resolve to a module file.
        add_if_local(f"{module_path}.py")

        # Candidate 2: or to a package whose implementation is __init__.py.
        add_if_local(os.path.join(module_path, "__init__.py"))

        # Include parent package initializers that may re-export the import.
        for index in range(1, len(components)):
            package_path = os.path.join(repo_path, *components[:index], "__init__.py")
            add_if_local(package_path)
    return found


# --- AST focus pass ----------------------------------------------------------
# Read-only, stdlib only. Adapted from the standalone ast_context_probe.py.


def _locations_from_text(text: str) -> set[tuple[str, int]]:
    found = set()
    for m in TRACEBACK_FILE_RE.finditer(text):
        found.add((m.group(1), int(m.group(2))))
    for m in FILE_LINE_RE.finditer(text):
        found.add((m.group("path"), int(m.group("line"))))
    return found


def _locations_from_junit_xml(xml_text: str) -> set[tuple[str, int]]:
    """Read Python file/line locations from JUnit failure/error entries."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return set()

    found: set[tuple[str, int]] = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in {"failure", "error"}:
            continue

        found |= _locations_from_text(" ".join(node.itertext()))

        path, line = node.get("file"), node.get("line")
        if path and line and line.isdigit():
            found.add((path, int(line)))
    return found


def find_failing_locations(test_output: str) -> set[tuple[str, int]]:
    """(path, line) pairs, from JUnit XML failure entries when available,
    otherwise from raw pytest output text."""
    if "<testsuite" in test_output or "<?xml" in test_output:
        found = _locations_from_junit_xml(test_output)
        if found:
            return found
    return _locations_from_text(test_output)


def _local_path(repo: Path, reported_path: str) -> Path | None:
    """Resolve /app or relative report paths, rejecting paths outside repo."""
    if "site-packages" in reported_path:
        return None
    path = reported_path.removeprefix("/app/")
    candidate = Path(path) if Path(path).is_absolute() else repo / path

    try:
        resolved = candidate.resolve()
        resolved.relative_to(repo.resolve())
    except (OSError, ValueError):
        return None

    return resolved if resolved.is_file() and resolved.suffix == ".py" else None


def _parse_python(path: Path) -> tuple[str, ast.Module] | None:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        return source, ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return None


def _enclosing_callable(tree: ast.AST, line: int) -> ast.AST | None:
    """Return the smallest function/class AST node containing this line."""
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, CALLABLES)
        and node.lineno <= line <= getattr(node, "end_lineno", node.lineno)
    ]
    return min(
        matches,
        key=lambda node: getattr(node, "end_lineno", node.lineno) - node.lineno,
        default=None,
    )


def _node_snippet(label: str, source: str, node: ast.AST) -> dict:
    lines = source.splitlines()
    start = min(
        [node.lineno]
        + [decorator.lineno for decorator in getattr(node, "decorator_list", [])]
    )
    end = getattr(node, "end_lineno", node.lineno)
    return {
        "label": label,
        "start": start,
        "end": end,
        "source": "\n".join(lines[start - 1 : end]),
    }


def _resolve_module(repo: Path, module: str) -> Path | None:
    """Resolve a dotted import to a local module or package __init__.py."""
    parts = module.split(".")
    module_file = repo.joinpath(*parts).with_suffix(".py")
    package_file = repo.joinpath(*parts, "__init__.py")

    for candidate in (module_file, package_file):
        if candidate.is_file():
            try:
                candidate.resolve().relative_to(repo.resolve())
            except ValueError:
                continue
            return candidate.resolve()
    return None


def _imported_definitions(
    repo: Path, test_tree: ast.Module, test_function: ast.AST
) -> list[tuple[Path, str]]:
    """Resolve called names imported into the failing test to local definitions."""
    called_names = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(test_function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    targets: set[tuple[Path, str]] = set()

    for node in ast.walk(test_tree):
        if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
            continue

        module_path = _resolve_module(repo, node.module)
        if not module_path:
            continue

        for alias in node.names:
            bound_name = alias.asname or alias.name
            if alias.name != "*" and bound_name in called_names:
                targets.add((module_path, alias.name))

    return sorted(targets, key=lambda item: (str(item[0]), item[1]))


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", "tests", "test"}
MAX_METHOD_MATCHES = 3


def _method_definitions(repo: Path, test_tree: ast.Module, test_function: ast.AST) -> list[tuple]:
    """Resolve ``obj.method`` uses (calls or references) in a failing test to ``def method`` in repo source.

    Matching is by name only. Definitions in modules the test imports win; otherwise a
    name defined in more than MAX_METHOD_MATCHES places is too generic and is skipped.
    """
    names = {node.attr for node in ast.walk(test_function) if isinstance(node, ast.Attribute)}
    imported = {
        module_path
        for node in ast.walk(test_tree)
        if isinstance(node, ast.ImportFrom) and node.module and not node.level
        for module_path in [_resolve_module(repo, node.module)]
        if module_path
    }
    found: dict[str, list[tuple]] = {name: [] for name in names}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in files:
            path = Path(root) / filename
            if not filename.endswith(".py") or filename.startswith("test_"):
                continue
            parsed = _parse_python(path)
            if not parsed or not any(f"def {name}(" in parsed[0] for name in names):
                continue
            source, tree = parsed
            for cls in ast.walk(tree):
                if isinstance(cls, ast.ClassDef):
                    for item in cls.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in names:
                            found[item.name].append((path.resolve(), f"Method {cls.name}.{item.name}", source, item))

    chosen: list[tuple] = []
    for matches in found.values():
        preferred = [m for m in matches if m[0] in imported]
        if preferred:
            chosen += preferred[:MAX_METHOD_MATCHES]
        elif len(matches) <= MAX_METHOD_MATCHES:
            chosen += matches
    return chosen


def _collect_ast_focus(repo_path: str, locations: set[tuple[str, int]]) -> dict[str, list[dict]]:
    """Map rel_path -> [snippet, ...]: the function/class enclosing each failure
    line, plus (for failing tests) definitions of imported names the test calls."""
    repo = Path(repo_path).resolve()
    focus: dict[str, list[dict]] = {}
    seen: set[tuple[str, int, int]] = set()

    def add(path: Path, snippet: dict) -> None:
        rel = path.relative_to(repo).as_posix()
        key = (rel, snippet["start"], snippet["end"])
        if key not in seen:
            seen.add(key)
            focus.setdefault(rel, []).append(snippet)

    for reported_path, line in sorted(locations):
        path = _local_path(repo, reported_path)
        if path is None:
            continue

        parsed = _parse_python(path)
        if not parsed:
            continue
        source, tree = parsed
        node = _enclosing_callable(tree, line)
        if node is None:
            continue
        add(path, _node_snippet("Enclosing AST node", source, node))

        # For a failing test, follow called `from module import symbol` names
        # to their definitions in the local repository.
        if "test" not in path.name.lower():
            continue

        for module_path, symbol in _imported_definitions(repo, tree, node):
            target = _parse_python(module_path)
            if not target:
                continue
            target_source, target_tree = target
            definition = next(
                (
                    item
                    for item in ast.walk(target_tree)
                    if isinstance(item, CALLABLES) and item.name == symbol
                ),
                None,
            )
            if definition:
                add(
                    module_path,
                    _node_snippet(f"Imported definition {symbol}", target_source, definition),
                )

        for module_path, label, method_source, method in _method_definitions(repo, tree, node):
            add(module_path, _node_snippet(label, method_source, method))
    return focus


def extract_failure_context(repo_path: str, test_output: str) -> list[dict]:
    """Returns a list of {"path": rel_path, "content": str, "focus": [...]} for
    every implicated file that actually exists in the local repo, plus any local
    modules those files import (see _local_imports). ``content`` is the full
    file; ``focus`` holds the AST snippets for that file (may be empty). The
    workflow prefers a freshly-generated JUnit XML report when present, but still
    falls back to raw pytest output parsing if needed."""
    rel_paths = set()
    locations = set()

    report_path = os.path.join(repo_path, "report.xml")
    if os.path.isfile(report_path):
        with open(report_path, "r", errors="replace") as f:
            report_text = f.read()
        rel_paths |= find_failing_files(report_text)
        locations |= find_failing_locations(report_text)

    rel_paths |= find_failing_files(test_output)
    locations |= find_failing_locations(test_output)

    # AST pass: failure line -> enclosing function/class (+ imported definitions
    # for failing tests). Files it finds join the set so they get the same
    # import expansion below.
    focus = _collect_ast_focus(repo_path, locations)
    rel_paths |= set(focus)

    # Expand with local imports found inside the already-identified files
    # (handles e.g. a test file whose imported implementation module never
    # appears in the traceback).
    for rel_path in list(rel_paths):
        abs_path = os.path.join(repo_path, rel_path)
        if os.path.isfile(abs_path) and rel_path.endswith(".py"):
            with open(abs_path, "r", errors="replace") as f:
                rel_paths |= _local_imports(f.read(), repo_path)

    contexts = []
    for rel_path in sorted(rel_paths):
        abs_path = os.path.join(repo_path, rel_path)
        if os.path.isfile(abs_path):
            with open(abs_path, "r", errors="replace") as f:
                content = f.read()
            contexts.append({"path": rel_path, "content": content, "focus": focus.get(rel_path, [])})
    return contexts