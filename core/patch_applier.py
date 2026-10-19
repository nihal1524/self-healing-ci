"""
C. Patch Application (File Writer)

Applies the LLM's search/replace edit to the LOCAL working copy (the same
directory that gets mounted into Docker on the next test run). `old` must
match the file exactly once; a .patch file is written for the record.
"""

import difflib
import os


def _closest_lines(text: str, old: str) -> str:
    """Hint for the model only: real file lines most similar to the missing snippet."""
    lines = text.splitlines()
    first = next((l.strip() for l in old.splitlines() if l.strip()), "")
    close = difflib.get_close_matches(first, [l.strip() for l in lines], n=3, cutoff=0.6)
    hits = [f"  line {i}: {l}" for i, l in enumerate(lines, 1) if l.strip() in close]
    return ". Closest lines actually in the file:\n" + "\n".join(hits[:3]) if hits else ""


def apply_patch(
    repo_path: str, file_path: str, old: str, new: str, patch_name: str = "fix.patch"
) -> dict:
    target = os.path.realpath(os.path.join(repo_path, file_path))
    patch_path = os.path.join(repo_path, patch_name)
    result = {"success": False, "method": "search/replace", "patch_file": patch_path}

    if not target.startswith(os.path.realpath(repo_path) + os.sep) or not os.path.isfile(target):
        return {**result, "output": f"File not found in repo: {file_path}"}

    if "".join(old.split()) == "".join(new.split()):
        return {**result, "output": f"Edit to {file_path} is a no-op (new equals old, ignoring whitespace); make a real change."}

    with open(target) as f:
        text = f.read()
    count = text.count(old)
    if count != 1:
        reason = f"snippet matched {count} places, add more surrounding lines" if count else "snippet not found" + _closest_lines(text, old)
        return {**result, "output": f"Edit to {file_path} failed: {reason}."}

    updated = text.replace(old, new)
    with open(target, "w") as f:
        f.write(updated)
    with open(patch_path, "w") as f:
        f.writelines(
            difflib.unified_diff(
                text.splitlines(True), updated.splitlines(True), f"a/{file_path}", f"b/{file_path}"
            )
        )
    return {**result, "success": True, "output": ""}
