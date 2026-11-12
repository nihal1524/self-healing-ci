"""Machine-readable summary of a heal.py run (read by the GitHub Action to build the PR)."""

import json


def build_result(state: dict, model: str) -> dict:
    plan = state.get("patch_plan")
    notes = [
        line[2:] for line in (state.get("feedback") or "").splitlines() if line.startswith("- Attempt")
    ]
    if state.get("status") == "success" and notes:
        notes = notes[:-1]  # the last applied attempt is the one that worked; only earlier ones "failed"
    return {
        "status": state.get("status"),
        "fixes_used": state.get("fixes_used"),
        "apply_failures": state.get("apply_failures"),
        "error": state.get("error"),
        "root_cause": plan.root_cause if plan else None,
        "attempt_notes": notes,
        "model": model,
    }


def write_result(path: str, result: dict) -> None:
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
