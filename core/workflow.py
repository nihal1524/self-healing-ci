"""LangGraph orchestration for the local self-healing test/patch loop."""

from typing import Callable, TypedDict

from langgraph.graph import END, StateGraph

from .context_extractor import extract_failure_context
from .library_context import library_contexts
from .models import PatchPlan
from .observability import traced
from .patch_applier import apply_patch
from .patch_generator import generate_patch


class WorkflowState(TypedDict):
    """Values passed between nodes during one repair run."""

    repo_path: str
    max_attempts: int
    model: str
    fixes_used: int
    test_output: str
    exit_code: int
    tests_passed: bool
    contexts: list[dict[str, str]]
    patch_plan: PatchPlan | None
    feedback: str
    apply_failures: int
    status: str
    error: str | None


MAX_APPLY_FAILURES = 2  # edits that fail to apply are retried with feedback this many times per run

# Metadata-only summaries for tracing (no prompts, file contents or test output).
def _summarize_tests(state, result):
    return {
        "status": result.get("status"),
        "passed": result.get("tests_passed"),
        "exit_code": result.get("exit_code"),
        "fixes_used": state["fixes_used"],
        "output_chars": len(result.get("test_output", "")),
    }


def _summarize_context(state, result):
    paths = [c["path"] for c in result.get("contexts", [])]
    return {
        "status": result.get("status"),
        "files": paths,
        "library_functions": [p for p in paths if p.startswith("external library")],
    }


def _summarize_plan(state, result):
    plan = result.get("patch_plan")
    return {
        "status": result.get("status"),
        "root_cause": plan.root_cause if plan else None,
        "edits": [{"file_path": p.file_path, "old": p.old, "new": p.new} for p in plan.patches] if plan else [],
    }


def _summarize_apply(state, result):
    return {
        "status": result.get("status"),
        "apply_failures": result.get("apply_failures", state["apply_failures"]),
        "fixes_used": result.get("fixes_used", state["fixes_used"]),
        "failure_feedback": result.get("feedback", "")[-1000:] if result.get("status") != "patches_applied" else None,
    }


def build_workflow(
    runner,
    max_attempts: int,
    model: str,
    context_extractor: Callable = extract_failure_context,
    patch_generator: Callable = generate_patch,
    patch_applier: Callable = apply_patch,
):
    """Build a compiled workflow, allowing services to be injected for tests."""

    def run_tests(state: WorkflowState) -> dict:
        fixes_used = state["fixes_used"]
        run_label = (
            "Final verification run"
            if fixes_used >= state["max_attempts"]
            else f"Fix attempt {fixes_used + 1}/{state['max_attempts']}"
        )
        print(f"\n{'=' * 60}\n{run_label}: running tests in Docker\n{'=' * 60}")
        try:
            result = runner.run()
        except Exception as exc:
            return {
                "status": "runner_error",
                "error": str(exc),
                "tests_passed": False,
            }

        output = result.get("output", "")
        exit_code = result.get("exit_code", 1)
        tests_passed = bool(result.get("success", False))
        print(output[-3000:])
        status = "tests_failed"
        if tests_passed and fixes_used == 0:
            status = "already_passing"
            print("\n⚠️ Tests passed on the first run: failure not reproduced, no fix needed.")
        elif tests_passed:
            status = "success"
            print("\n✅ Tests passed!")
        return {
            "test_output": output,
            "exit_code": exit_code,
            "tests_passed": tests_passed,
            "status": status,
            "error": None,
        }

    def route_after_tests(state: WorkflowState) -> str:
        if state["tests_passed"]:
            return "done"
        if state["status"] == "runner_error":
            return "done"
        if state["fixes_used"] >= state["max_attempts"]:
            return "exhausted"
        return "extract"

    def mark_exhausted(state: WorkflowState) -> dict:
        message = (
            f"Final verification failed after {state['max_attempts']} fix attempt(s) "
            f"(exit code {state['exit_code']})."
        )
        print(f"\n❌ {message}")
        return {"status": "verification_failed", "error": message}

    def extract_context(state: WorkflowState) -> dict:
        print(f"\n❌ Tests failed (exit code {state['exit_code']}). Extracting context...")
        try:
            contexts = context_extractor(state["repo_path"], state["test_output"])
        except Exception as exc:
            message = f"Context extraction failed: {exc}"
            print(message)
            return {"contexts": [], "status": "context_error", "error": message}
        if not contexts:
            message = "Could not locate the failing source file(s) from the test output."
            print(message)
            return {"contexts": [], "status": "no_context", "error": message}
        print(f"Found {len(contexts)} relevant file(s): {[item['path'] for item in contexts]}")
        try:
            libraries = library_contexts(runner, state["repo_path"], contexts)
        except Exception as exc:
            libraries = []
            print(f"Skipping external library lookup: {exc}")
        if libraries:
            print(f"Added {len(libraries)} external library function(s): {[item['path'] for item in libraries]}")
            contexts = contexts + libraries
        return {"contexts": contexts, "status": "context_found", "error": None}

    def route_after_context(state: WorkflowState) -> str:
        return "generate" if state["contexts"] else "done"

    def generate_plan(state: WorkflowState) -> dict:
        print(f"Asking {state['model']} to diagnose and propose a fix...")
        try:
            plan = patch_generator(
                state["test_output"],
                state["contexts"],
                model=state["model"],
                feedback=state["feedback"],
            )
        except Exception as exc:
            message = f"LLM call failed: {exc}"
            print(message)
            return {"patch_plan": None, "status": "llm_error", "error": message}
        print(f"\nRoot cause: {plan.root_cause}")
        return {"patch_plan": plan, "status": "plan_generated", "error": None}

    def route_after_generation(state: WorkflowState) -> str:
        return "apply" if state["patch_plan"] is not None else "done"

    def apply_plan(state: WorkflowState) -> dict:
        plan = state["patch_plan"]
        if plan is None or not plan.patches:
            message = "LLM returned an empty patch plan; stopping without rerunning tests."
            print(message)
            return {"status": "patch_error", "error": message}

        patch_attempt = state["fixes_used"] + 1
        for index, patch in enumerate(plan.patches, start=1):
            print(f"\n[{index}/{len(plan.patches)}] Patching {patch.file_path}")
            print(f"    Why: {patch.explanation}")
            try:
                result = patch_applier(
                    state["repo_path"],
                    patch.file_path,
                    patch.old,
                    patch.new,
                    patch_name=f"attempt{patch_attempt}_{index}.patch",
                )
            except Exception as exc:
                result = {"success": False, "output": f"Patch application raised an error: {exc}"}
            if result.get("success"):
                print(f"    Applied -> {result['patch_file']}")
                continue

            error = result.get("output", "")
            print(f"    {error}\n    Rejected snippet:\n{patch.old}")
            failures = state["apply_failures"] + 1
            feedback = state["feedback"] + (
                f"- Edit to {patch.file_path} could not be applied: {error}\n"
                f"  old snippet was:\n{patch.old}\n"
            )
            if failures >= MAX_APPLY_FAILURES:
                message = "Patch failed to apply and no attempts are left. Stopping."
                print(f"\n{message}")
                return {"status": "patch_error", "error": message, "apply_failures": failures}
            return {"status": "patch_retry", "feedback": feedback, "apply_failures": failures}

        feedback = state["feedback"] + f"- Attempt {patch_attempt}: {plan.root_cause} (applied, tests still failed)\n"
        return {
            "fixes_used": patch_attempt,
            "status": "patches_applied",
            "feedback": feedback,
            "error": None,
        }

    def route_after_apply(state: WorkflowState) -> str:
        if state["status"] == "patch_retry":
            return "extract"
        return "run_tests" if state["status"] == "patches_applied" else "done"

    graph = StateGraph(WorkflowState)
    graph.add_node("run_tests", traced("run-tests", _summarize_tests)(run_tests))
    graph.add_node("mark_exhausted", mark_exhausted)
    graph.add_node("extract_context", traced("extract-context", _summarize_context)(extract_context))
    graph.add_node("generate_plan", traced("generate-plan", _summarize_plan)(generate_plan))
    graph.add_node("apply_plan", traced("apply-plan", _summarize_apply)(apply_plan))

    graph.set_entry_point("run_tests")
    graph.add_conditional_edges(
        "run_tests",
        route_after_tests,
        {"done": END, "exhausted": "mark_exhausted", "extract": "extract_context"},
    )
    graph.add_edge("mark_exhausted", END)
    graph.add_conditional_edges(
        "extract_context",
        route_after_context,
        {"generate": "generate_plan", "done": END},
    )
    graph.add_conditional_edges(
        "generate_plan",
        route_after_generation,
        {"apply": "apply_plan", "done": END},
    )
    graph.add_conditional_edges(
        "apply_plan",
        route_after_apply,
        {"run_tests": "run_tests", "extract": "extract_context", "done": END},
    )
    return graph.compile()


def run_workflow(
    runner,
    repo_path: str,
    max_attempts: int,
    model: str,
    **dependencies,
) -> WorkflowState:
    """Run the graph once and return its final state."""
    app = build_workflow(runner, max_attempts, model, **dependencies)
    return app.invoke(
        {
            "repo_path": repo_path,
            "max_attempts": max_attempts,
            "model": model,
            "fixes_used": 0,
            "test_output": "",
            "exit_code": 0,
            "tests_passed": False,
            "contexts": [],
            "patch_plan": None,
            "feedback": "",
            "apply_failures": 0,
            "status": "started",
            "error": None,
        }
    )