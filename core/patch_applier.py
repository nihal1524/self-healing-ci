"""
C. Patch Application (File Writer)

Writes the LLM's unified diff to a .patch file inside the repo and applies
it against the LOCAL working copy (the same directory that gets mounted
into Docker on the next test run), so the container sees the fix.

Tries `git apply` first (works even in a non-git directory if you init one;
falls back to the POSIX `patch` command otherwise).
"""

import os
import subprocess


def apply_patch(repo_path: str, diff_text: str, patch_name: str = "fix.patch") -> dict:
    patch_path = os.path.join(repo_path, patch_name)
    with open(patch_path, "w") as f:
        f.write(diff_text)
        if not diff_text.endswith("\n"):
            f.write("\n")

    git_result = subprocess.run(
        ["git", "apply", "--verbose", patch_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if git_result.returncode == 0:
        return {
            "success": True,
            "method": "git apply",
            "output": git_result.stdout or git_result.stderr,
            "patch_file": patch_path,
        }

    patch_result = subprocess.run(
        ["patch", "-p1", "--fuzz=3", "-i", patch_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return {
        "success": patch_result.returncode == 0,
        "method": "patch -p1",
        "output": patch_result.stdout + patch_result.stderr,
        "git_apply_error": git_result.stderr,
        "patch_file": patch_path,
    }
