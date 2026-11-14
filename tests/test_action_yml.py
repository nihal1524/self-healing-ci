import json
import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ACTION = Path(__file__).resolve().parents[1] / "action.yml"


def step_script(name):
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == name)


def run_block(script, tmp_path, **env):
    out = tmp_path / "gh_output"
    out.write_text("")
    full_env = {**os.environ, "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": str(tmp_path), **env}
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                          env=full_env, capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return proc, outputs


def test_guard_skips_self_healing_branches_to_avoid_loops(tmp_path):
    script = step_script("Resolve branch and loop guard")

    _, healed = run_block(script, tmp_path, INPUT_BRANCH="self-healing/feature/x", REF_NAME="main")
    _, normal = run_block(script, tmp_path, INPUT_BRANCH="", REF_NAME="feature/x")

    assert healed == {"branch": "self-healing/feature/x", "skip": "true"}
    assert normal == {"branch": "feature/x", "skip": "false"}


def test_heal_step_passes_inputs_and_exposes_status(tmp_path):
    action_dir = tmp_path / "action"
    action_dir.mkdir()
    (action_dir / "heal.py").write_text(
        "import json, sys\n"
        "a = sys.argv\n"
        "json.dump({'status': 'success', 'args': a[1:]}, open(a[a.index('--result-file') + 1], 'w'))\n"
        "sys.exit(0)\n"
    )
    proc, outputs = run_block(
        step_script("Diagnose and fix"), tmp_path,
        GITHUB_ACTION_PATH=str(action_dir), GITHUB_WORKSPACE="/ws",
        TEST_CMD="pytest -q", SETUP_CMD="pip install -r requirements.txt", IMAGE="python:3.11",
        MAX_ATTEMPTS="2", MODEL="m", REPOSITORY="o/r",
    )

    assert proc.returncode == 0, proc.stderr
    assert outputs["status"] == "success" and outputs["exit_code"] == "0"
    args = json.loads(Path(outputs["result_file"]).read_text())["args"]
    assert args[:1] == ["/ws"]
    for expected in (["--test-cmd", "pytest -q"], ["--setup-cmd", "pip install -r requirements.txt"],
                     ["--image", "python:3.11"], ["--max-attempts", "2"], ["--trace-tag", "gha:o/r"]):
        assert args[args.index(expected[0]) + 1] == expected[1]


def test_optional_flags_are_left_out_when_empty_and_a_crash_does_not_abort_the_step(tmp_path):
    action_dir = tmp_path / "action"
    action_dir.mkdir()
    (action_dir / "heal.py").write_text("import sys\nsys.exit(1)\n")  # crashes, writes no result file

    proc, outputs = run_block(
        step_script("Diagnose and fix"), tmp_path,
        GITHUB_ACTION_PATH=str(action_dir), GITHUB_WORKSPACE="/ws",
        TEST_CMD="pytest", SETUP_CMD="", IMAGE="", MAX_ATTEMPTS="3", MODEL="m", REPOSITORY="o/r",
    )

    assert proc.returncode == 0, proc.stderr          # the step itself survives, so later steps can report
    assert outputs["status"] == "unknown" and outputs["exit_code"] == "1"
