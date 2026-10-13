"""
B. Context Extraction (File Reader)

Parses pytest/npm test output to figure out which source file(s) are
implicated in the failure, then reads their full content from the LOCAL
repo path (not from inside the container) so it can be handed to the LLM
as context.

This uses lightweight regexes rather than a full test-report parser to
keep Phase 1 dependency-free. It's intentionally permissive: it's fine to
over-collect a few extra files, the LLM will sort out what's relevant.
"""

import os
import re
import xml.etree.ElementTree as ET

# pytest short summary line: "FAILED tests/test_foo.py::test_bar - AssertionError"
PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+?)(?:::\S+)?\s", re.MULTILINE)

# Python traceback frames: File "/app/src/foo.py", line 12, in bar
TRACEBACK_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')

# Node/Jest style: "at Object.<anonymous> (/app/src/foo.js:12:5)"
JS_STACK_RE = re.compile(r"\(?(/app/[^\s:()]+\.[jt]sx?):(\d+):(\d+)\)?")


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


def extract_failure_context(repo_path: str, test_output: str, max_chars: int = 6000) -> list[dict]:
    """Returns a list of {"path": rel_path, "content": str} for every
    implicated file that actually exists in the local repo, plus any local
    modules those files import (see _local_imports). The workflow prefers a
    freshly-generated JUnit XML report when present, but still falls back to
    raw pytest output parsing if needed."""
    rel_paths = set()

    report_path = os.path.join(repo_path, "report.xml")
    if os.path.isfile(report_path):
        with open(report_path, "r", errors="replace") as f:
            rel_paths |= find_failing_files(f.read())

    rel_paths |= find_failing_files(test_output)

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
            contexts.append({"path": rel_path, "content": content[:max_chars]})
    return contexts
