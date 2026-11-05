from core.context_slice import reduce_file
from core.patch_generator import MAX_CONTEXT_CHARS, fit_budget, format_context

SOURCE = '''import os
from math import sqrt

USED = 10
UNUSED = 99


def helper(x):
    return x + USED


def unrelated_one(x):
    marker_unrelated = 1
    return x


class Other:
    def method(self):
        return "other body"


class Target:
    flag = True

    def __init__(self):
        self.size = 3

    def failing(self, y):
        return helper(y) + sqrt(2)

    def sibling(self):
        sibling_body = 1
        return sibling_body
'''


def focus_for(source, text):
    lines = source.splitlines()
    first = next(i for i, l in enumerate(lines, 1) if text in l)
    return [{"label": "Enclosing AST node", "start": first, "end": first + 1, "source": ""}]


def test_reduced_view_keeps_what_the_failing_code_uses():
    reduced = reduce_file(SOURCE, focus_for(SOURCE, "def failing"))

    for kept in ("import os", "from math import sqrt", "USED = 10", "return x + USED",
                 "class Target:", "flag = True", "self.size = 3", "return helper(y) + sqrt(2)",
                 "def sibling(self):"):
        assert kept in reduced
    for dropped in ("UNUSED = 99", "marker_unrelated", "sibling_body", '"other body"'):
        assert dropped not in reduced
    assert "lines omitted" in reduced and "class Other" in reduced


def test_large_file_is_reduced_and_small_file_is_sent_whole():
    big = SOURCE + "\n".join(f"def filler_{i}():\n    return {i}\n" for i in range(500))
    context = {"path": "m.py", "content": big, "focus": focus_for(big, "def failing")}

    reduced = format_context(context)
    assert "Reduced view" in reduced and "def filler_7():" in reduced  # signature kept as an index
    assert "    return 7\n" not in reduced  # body dropped
    assert "[Full file]" in format_context({**context, "content": SOURCE})


def test_over_budget_drops_largest_file_without_focus_first():
    focused = {"path": "a.py", "content": "x = 1\n", "focus": [{"label": "L", "start": 1, "end": 1, "source": "x = 1"}]}
    huge = {"path": "huge.txt", "content": "y" * (MAX_CONTEXT_CHARS + 10), "focus": []}

    kept = [c["path"] for c, _ in fit_budget([focused, huge])]

    assert kept == ["a.py"]
