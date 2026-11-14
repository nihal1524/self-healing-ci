import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "open_pr.sh"

FAKE_GH = """#!/usr/bin/env bash
echo "$@" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr list") [ -f "$FAKE_GH_EXISTING" ] && cat "$FAKE_GH_EXISTING" ;;
  "pr view") echo "https://github.com/o/r/pull/7" ;;
esac
exit 0
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def setup(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "feature/login", str(work)], check=True)
    git(work, "config", "user.name", "t")
    git(work, "config", "user.email", "t@t")
    (work / "app.py").write_text("x = 1\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "feature/login")
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    result = tmp_path / "result.json"
    result.write_text(json.dumps({
        "status": "success", "fixes_used": 1, "apply_failures": 0, "root_cause": "x should be 2",
        "attempt_notes": ["Attempt 1: x should be 2 (applied, tests still failed)"], "model": "m",
    }))
    env = {**os.environ, "HEAL_BRANCH": "feature/login", "HEAL_RESULT_FILE": str(result), "GH_BIN": str(gh),
           "FAKE_GH_LOG": str(tmp_path / "gh.log"), "FAKE_GH_EXISTING": str(tmp_path / "existing")}
    return work, remote, env


def run(work, env):
    return subprocess.run(["bash", str(SCRIPT)], cwd=work, env=env, capture_output=True, text=True)


def test_fix_is_committed_to_the_self_healing_branch_and_a_pr_is_opened(tmp_path):
    work, remote, env = setup(tmp_path)
    (work / "app.py").write_text("x = 2\n")                 # the model's edit
    (work / "attempt1_1.patch").write_text("noise\n")       # heal.py artifact, untracked

    result = run(work, env)

    assert result.returncode == 0, result.stderr
    assert git(remote, "branch", "--list", "self-healing/feature/login")  # pushed
    files = git(remote, "show", "--name-only", "--format=", "self-healing/feature/login").split()
    assert files == ["app.py"]                              # patch artifact not committed
    log = (tmp_path / "gh.log").read_text()
    assert "pr create --base feature/login --head self-healing/feature/login" in log
    assert "pr edit" not in log


def test_existing_pr_is_updated_instead_of_duplicated(tmp_path):
    work, remote, env = setup(tmp_path)
    (tmp_path / "existing").write_text("42\n")
    (work / "app.py").write_text("x = 2\n")

    result = run(work, env)

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "gh.log").read_text()
    assert "pr edit 42" in log and "pr create" not in log


def test_nothing_to_commit_means_no_branch_and_no_pr(tmp_path):
    work, remote, env = setup(tmp_path)

    result = run(work, env)

    assert result.returncode == 0 and "No source changes" in result.stdout
    assert git(remote, "branch", "--list", "self-healing/*") == ""
    assert not (tmp_path / "gh.log").exists()
