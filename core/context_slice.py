"""
Reduced view of a large source file for the model's prompt.

Keeps (verbatim, so exact-match edits still work): imports, the failing code in
full, module-level names that code uses, class shells (header, class attributes,
method signatures) and everything else is replaced by "# ... [N lines omitted]".
"""

import ast

FULL_FILE_MAX_LINES = 800  # files up to this size are sent whole
HELPER_MAX_LINES = 60      # a used helper function is shown in full up to this size
INIT_MAX_LINES = 40        # __init__ shows which attributes self gets


def reduce_file(content: str, focus: list[dict]) -> str:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return content
    lines = content.splitlines()
    ranges = [(f["start"], f["end"]) for f in focus]
    keep: set[int] = set()

    def add(first, last):
        keep.update(range(first, last + 1))

    def start(node):
        return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])

    def in_focus(node):
        return any(s <= node.lineno and node.end_lineno <= e for s, e in ranges)

    def header(node):  # decorators + signature / class line, without the body
        add(start(node), max(node.lineno, node.body[0].lineno - 1))

    used = {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and any(s <= n.lineno <= e for s, e in ranges)
    }

    # Names used by the helper functions we will keep in full (one level deeper).
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in used
            and node.end_lineno - node.lineno <= HELPER_MAX_LINES
        ):
            used |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def shell(cls):
        header(cls)
        for item in cls.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                add(item.lineno, item.end_lineno)
            elif isinstance(item, ast.ClassDef):
                shell(item)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                short_init = item.name == "__init__" and item.end_lineno - item.lineno <= INIT_MAX_LINES
                if in_focus(item) or short_init:
                    add(start(item), item.end_lineno)
                else:
                    header(item)

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            add(node.lineno, node.end_lineno)
        elif isinstance(node, (ast.If, ast.Try)):  # conditional imports
            if node.end_lineno - node.lineno <= 15 and any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(node)):
                add(node.lineno, node.end_lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if {t.id for t in targets if isinstance(t, ast.Name)} & used or in_focus(node):
                add(node.lineno, node.end_lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if in_focus(node) or (node.name in used and node.end_lineno - node.lineno <= HELPER_MAX_LINES):
                add(start(node), node.end_lineno)
            else:
                header(node)
        elif isinstance(node, ast.ClassDef):
            if in_focus(node):
                add(start(node), node.end_lineno)
            elif node.name in used or any(in_focus(n) for n in ast.walk(node) if hasattr(n, "end_lineno") and hasattr(n, "lineno")):
                shell(node)
            else:
                header(node)

    out, skipped = [], 0
    for number, line in enumerate(lines, 1):
        if number in keep:
            if skipped:
                out.append(f"# ... [{skipped} lines omitted]")
                skipped = 0
            out.append(line)
        else:
            skipped += 1
    if skipped:
        out.append(f"# ... [{skipped} lines omitted]")
    return "\n".join(out)
