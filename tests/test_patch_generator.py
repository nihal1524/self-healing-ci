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


def _fake_anthropic(monkeypatch, tool_inputs):
    import types

    import core.patch_generator as pg

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        block = types.SimpleNamespace(type="tool_use", name="submit_patch_plan", input=tool_inputs[len(calls) - 1])
        return types.SimpleNamespace(
            content=[block], usage=types.SimpleNamespace(input_tokens=10, output_tokens=5), stop_reason="tool_use"
        )

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(pg.anthropic, "Anthropic", lambda api_key: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    return pg, calls


GOOD = {
    "root_cause": "bug",
    "patches": [{"file_path": "a.py", "explanation": "why", "old": "x = 1", "new": "x = 2"}],
}


def test_invalid_tool_input_is_retried_with_the_error(monkeypatch):
    bad = {"root_cause": "bug", "patches": '<parameter name="file_path">a.py'}
    pg, calls = _fake_anthropic(monkeypatch, [bad, GOOD])

    plan = pg.generate_patch("out", [{"path": "a.py", "content": "x = 1"}])

    assert plan.patches[0].new == "x = 2"
    assert len(calls) == 2
    assert "previous response was invalid" in calls[1]["messages"][0]["content"][1]["text"]


def test_gives_up_after_repeated_invalid_tool_input(monkeypatch):
    import pytest

    bad = {"root_cause": "bug", "patches": "oops"}
    pg, calls = _fake_anthropic(monkeypatch, [bad, bad, bad])

    with pytest.raises(RuntimeError, match="3 tries"):
        pg.generate_patch("out", [{"path": "a.py", "content": "x = 1"}])
    assert len(calls) == 3


def test_large_context_is_the_cacheable_prefix_and_retries_keep_it_identical(monkeypatch):
    bad = {"root_cause": "bug", "patches": "oops"}
    pg, calls = _fake_anthropic(monkeypatch, [bad, GOOD])

    pg.generate_patch("TEST OUTPUT", [{"path": "a.py", "content": "x = 1"}], feedback="tried A")

    first, retry = (c["messages"][0]["content"] for c in calls)
    assert first[0]["cache_control"] == {"type": "ephemeral"}
    assert "Relevant source files" in first[0]["text"] and "TEST OUTPUT" not in first[0]["text"]
    assert "cache_control" not in first[1] and "TEST OUTPUT" in first[1]["text"] and "tried A" in first[1]["text"]
    assert retry[0] == first[0]  # byte-identical prefix -> the retry reads it from cache
    assert "previous response was invalid" in retry[1]["text"]
