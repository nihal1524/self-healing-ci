"""
LLM call: pass the error output + file content to Claude and get back a
PatchPlan (Pydantic model) via structured output.

We implement "structured outputs via Pydantic" by handing Claude the
PatchPlan JSON schema as a tool definition and forcing tool_choice, so the
API response is guaranteed to match the schema (or the call errors out) --
no fragile prompt-and-hope JSON parsing.
"""

import os
import anthropic

from .models import PatchPlan

SYSTEM_PROMPT = """You are an expert software engineer operating inside an \
automated self-healing CI pipeline. You will be given the output of a \
failing test run and the current contents of the source file(s) involved.

Diagnose the root cause and produce the SMALLEST correct fix. Respond only \
by calling the submit_patch_plan tool. Each unified_diff must:
  - start with '--- a/<path>' and '+++ b/<path>' headers
  - use the exact relative file path given in the context
  - apply cleanly with `git apply` from the repository root
  - contain only the minimal hunks needed to fix the failure

Do not modify the test file unless the test itself is provably wrong.
"""


def generate_patch(
    test_output: str,
    file_contexts: list[dict],
    model: str = "claude-sonnet-4-5-20250929",
) -> PatchPlan:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)

    context_text = "\n\n".join(
        f"--- FILE: {c['path']} ---\n{c['content']}" for c in file_contexts
    )

    tool = {
        "name": "submit_patch_plan",
        "description": "Submit the diagnosed root cause and the patch(es) that fix the failing test.",
        "input_schema": PatchPlan.model_json_schema(),
    }

    message = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_patch_plan"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Failing test output:\n```\n{test_output[-6000:]}\n```\n\n"
                    f"Relevant source files:\n{context_text}\n\n"
                    "Diagnose and produce a patch."
                ),
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_patch_plan":
            return PatchPlan.model_validate(block.input)

    raise RuntimeError("Model did not return the expected submit_patch_plan tool call")
