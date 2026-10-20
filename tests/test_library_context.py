from core.library_context import MARKER, external_calls, library_contexts

SOURCE = (
    "from extlib import helper\n"
    "from pkg.local import util\n"
    "class C:\n"
    "    def run(self, x):\n"
    "        return util(helper(x))\n"
)


def contexts():
    snippet = {"label": "Method C.run", "start": 4, "end": 5, "source": "    def run(self, x):\n        return util(helper(x))"}
    return [{"path": "pkg/c.py", "content": SOURCE, "focus": [snippet]}]


def repo_with_local_module(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "local.py").write_text("def util(x):\n    return x\n")
    return str(tmp_path)


def test_only_non_repo_called_functions_are_looked_up(tmp_path):
    assert external_calls(repo_with_local_module(tmp_path), contexts()) == [("extlib", "helper")]


def test_library_source_becomes_read_only_context(tmp_path):
    class Runner:
        def run_command(self, command):
            assert "extlib" in command
            return {"output": f'noise\n{MARKER}{{"extlib.helper": "def helper(x):\\n    return x\\n"}}\n'}

    result = library_contexts(Runner(), repo_with_local_module(tmp_path), contexts())

    assert result == [
        {"path": "external library, read-only: extlib.helper", "content": "def helper(x):\n    return x\n", "focus": []}
    ]


def test_runner_without_run_command_is_skipped(tmp_path):
    assert library_contexts(object(), repo_with_local_module(tmp_path), contexts()) == []
