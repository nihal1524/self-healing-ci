"""
One level deeper: show the source of third-party functions called by the code
under repair. The libraries only exist inside the Docker image, so their source
is read from a container (see DockerTestRunner.run_command).
"""

import ast
import json
import shlex
import textwrap
from pathlib import Path

from .context_extractor import _resolve_module

MAX_FUNCTIONS = 8
MAX_LINES = 80
MARKER = "@@LIB@@"

# Runs in the container: print the source of each module.name that lives in site-packages.
_FETCH_SCRIPT = f"""
import importlib, inspect, json, sys
out = {{}}
for module, name in json.loads(sys.argv[1]):
    try:
        obj = getattr(importlib.import_module(module), name)
        if "site-packages" in (inspect.getsourcefile(obj) or ""):
            out[module + "." + name] = "".join(inspect.getsourcelines(obj)[0][:{MAX_LINES}])
    except Exception:
        pass
print("{MARKER}" + json.dumps(out))
"""


def external_calls(repo_path: str, contexts: list[dict]) -> list[tuple[str, str]]:
    """(module, name) of non-repo functions that the focused source snippets call."""
    repo = Path(repo_path).resolve()
    targets: list[tuple[str, str]] = []
    for ctx in contexts:
        snippets = [f for f in ctx.get("focus", []) if f["label"].startswith(("Method ", "Imported definition"))]
        if not snippets:
            continue
        try:
            file_tree = ast.parse(ctx["content"])
        except SyntaxError:
            continue
        imports = {
            alias.asname or alias.name: (node.module, alias.name)
            for node in ast.walk(file_tree)
            if isinstance(node, ast.ImportFrom) and node.module and not node.level
            and not _resolve_module(repo, node.module)
            for alias in node.names
            if alias.name != "*"
        }
        for snippet in snippets:
            try:
                snippet_tree = ast.parse(textwrap.dedent(snippet["source"]))
            except SyntaxError:
                continue
            for call in ast.walk(snippet_tree):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in imports:
                    if imports[call.func.id] not in targets:
                        targets.append(imports[call.func.id])
    return targets[:MAX_FUNCTIONS]


def library_contexts(runner, repo_path: str, contexts: list[dict]) -> list[dict]:
    """Read-only context entries holding the source of the external functions called."""
    targets = external_calls(repo_path, contexts)
    if not targets or not hasattr(runner, "run_command"):
        return []
    result = runner.run_command(f"python -c {shlex.quote(_FETCH_SCRIPT)} {shlex.quote(json.dumps(targets))}")
    _, marker, payload = result.get("output", "").rpartition(MARKER)
    if not marker:
        return []
    return [
        {"path": f"external library, read-only: {name}", "content": source, "focus": []}
        for name, source in json.loads(payload.splitlines()[0]).items()
    ]
