#!/usr/bin/env python3
"""
Self-Healing CI/CD -- Phase 1 CLI

Usage:
    python heal.py /path/to/repo
    python heal.py ./sample-failing-repo --max-attempts 3

`--max-attempts` limits patch attempts; the pipeline runs one additional
test run to verify the final patch.

Flow per attempt:
    1. Run the test suite for the repo inside a Docker container
       (repo mounted read-write at /app).
    2. If it fails, extract the failing file(s) from the output and read
       their content from the local repo.
    3. Send the error + file content to Claude, asking for a structured
       PatchPlan (Pydantic model) via a forced tool call.
    4. Write out a .patch file and apply it with git apply / patch.
    5. Re-run the tests after each patch, including one final verification
       run after the last permitted patch attempt.
"""

import argparse
import os
import sys

from core import observability
from core.docker_runner import DockerTestRunner
from core.workflow import run_workflow


def main():
    parser = argparse.ArgumentParser(
        description="Self-healing CI: auto-diagnose and patch a failing test in a local repo."
    )
    parser.add_argument("repo_path", help="Path to the local repo containing the failing test")
    parser.add_argument("--image", default=None, help="Override the Docker base image")
    parser.add_argument(
        "--platform",
        default=None,
        help="Docker target platform (for example, linux/amd64 on Apple Silicon)",
    )
    parser.add_argument("--test-cmd", default=None, help="Override the test command run in-container")
    parser.add_argument(
        "--setup-cmd",
        default=None,
        help="Shell command to run once while building a cached environment image",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum patch attempts (plus one final verification run)",
    )
    parser.add_argument(
        "--model", default="claude-sonnet-5-5", help="Anthropic model to use for patch generation"
    )
    parser.add_argument(
        "--trace-tag",
        action="append",
        default=[],
        help="Langfuse tag for this run (repeatable), e.g. the BugsInPy bug id",
    )
    args = parser.parse_args()

    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")

    repo_path = os.path.abspath(args.repo_path)
    if not os.path.isdir(repo_path):
        print(f"Error: '{repo_path}' is not a directory")
        sys.exit(1)

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("Error: set the ANTHROPIC_API_KEY environment variable first")
        sys.exit(1)

    try:
        runner = DockerTestRunner(
            repo_path,
            image=args.image,
            test_command=args.test_cmd,
            setup_command=args.setup_cmd,
            platform=args.platform,
        )
    except Exception as e:
        print(f"Error initializing Docker: {e}")
        print("Is Docker Desktop / the Docker daemon running?")
        sys.exit(1)

    print(f"Repo:  {repo_path}")
    print(f"Image: {runner.image}")
    print(f"Test:  {runner.test_command}")
    print("Workflow: LangGraph")

    try:
        with observability.run_trace(
            "self-heal",
            tags=args.trace_tag,
            metadata={"repo": os.path.basename(repo_path), "model": args.model, "max_attempts": args.max_attempts},
        ) as run:
            final_state = run_workflow(
                runner=runner,
                repo_path=repo_path,
                max_attempts=args.max_attempts,
                model=args.model,
            )
            run.update(
                output={key: final_state.get(key) for key in ("status", "fixes_used", "apply_failures", "error")},
                level="DEFAULT" if final_state["status"] in ("success", "already_passing") else "ERROR",
            )
    except Exception as e:
        print(f"Workflow failed unexpectedly: {e}")
        sys.exit(1)
    finally:
        observability.flush()

    if final_state["status"] not in ("success", "already_passing"):
        if final_state.get("error"):
            print(f"\n❌ {final_state['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
