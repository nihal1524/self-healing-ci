from core.patch_applier import apply_patch


def test_edit_replaces_unique_snippet(tmp_path):
    target = tmp_path / "calculator.py"
    target.write_text("def add(a, b):\n    return 1\n")

    result = apply_patch(str(tmp_path), "calculator.py", "return 1", "return a + b", "fix.patch")

    assert result["success"] is True
    assert target.read_text() == "def add(a, b):\n    return a + b\n"
    assert "+    return a + b" in (tmp_path / "fix.patch").read_text()


def test_missing_or_ambiguous_snippet_is_rejected_without_changing_target(tmp_path):
    target = tmp_path / "calculator.py"
    original = "x = 1\ny = 1\n"
    target.write_text(original)

    missing = apply_patch(str(tmp_path), "calculator.py", "z = 1", "z = 2")
    ambiguous = apply_patch(str(tmp_path), "calculator.py", " = 1", " = 2")

    assert missing["success"] is False and "not found" in missing["output"]
    assert ambiguous["success"] is False and "2 places" in ambiguous["output"]
    assert target.read_text() == original


def test_path_outside_repo_is_rejected(tmp_path):
    (tmp_path / "outside.py").write_text("a")
    repo = tmp_path / "repo"
    repo.mkdir()

    assert apply_patch(str(repo), "../outside.py", "a", "b")["success"] is False


def test_noop_edit_is_rejected(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")

    result = apply_patch(str(tmp_path), "a.py", "x = 1", "x =  1")

    assert result["success"] is False and "no-op" in result["output"]


def test_not_found_message_shows_closest_real_lines(tmp_path):
    (tmp_path / "a.py").write_text("def f(a, b):\n    return a\n")

    result = apply_patch(str(tmp_path), "a.py", "def f(a, b, c=None):", "def f(a):")

    assert result["success"] is False
    assert "line 1: def f(a, b):" in result["output"]
