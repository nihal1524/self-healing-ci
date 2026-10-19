from core.patch_generator import format_context


def test_focus_snippets_come_before_full_file():
    text = format_context(
        {
            "path": "a.py",
            "content": "def f():\n    pass\n",
            "focus": [{"label": "Enclosing AST node", "start": 1, "end": 2, "source": "def f():\n    pass"}],
        }
    )

    assert text.index("Most relevant: Enclosing AST node, lines 1-2") < text.index("[Full file]")
    assert text.startswith("--- FILE: a.py ---")


def test_context_without_focus_still_formats():
    assert format_context({"path": "a.py", "content": "x"}).endswith("[Full file]\nx")
