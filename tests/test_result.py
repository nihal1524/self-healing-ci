import json
from types import SimpleNamespace

from core.result import build_result, write_result


def test_result_summarizes_final_state(tmp_path):
    state = {
        "status": "success",
        "fixes_used": 2,
        "apply_failures": 1,
        "error": None,
        "patch_plan": SimpleNamespace(root_cause="off by one in utils"),
        "feedback": "- Attempt 1: first guess (applied, tests still failed)\n- Edit to a.py could not be applied: x\n",
    }

    result = build_result(state, "m")
    write_result(str(tmp_path / "r.json"), result)

    assert json.loads((tmp_path / "r.json").read_text()) == result
    assert result["status"] == "success" and result["root_cause"] == "off by one in utils"
    assert result["attempt_notes"] == []  # the single applied attempt was the successful one


def test_result_without_a_plan():
    result = build_result({"status": "already_passing", "fixes_used": 0}, "m")

    assert result["root_cause"] is None and result["attempt_notes"] == []


def test_earlier_failed_attempts_are_listed_but_not_the_successful_one():
    state = {
        "status": "success",
        "feedback": "- Attempt 1: wrong idea (applied, tests still failed)\n- Attempt 2: right idea (applied, tests still failed)\n",
    }

    assert build_result(state, "m")["attempt_notes"] == ["Attempt 1: wrong idea (applied, tests still failed)"]
