"""
LLM call: pass the error output + file content to Claude and get back a
PatchPlan (Pydantic model) via structured output.

We implement "structured outputs via Pydantic" by handing Claude the
PatchPlan JSON schema as a tool definition (tool_choice "auto" -- this model
rejects forced tool calls; the prompt requires the tool call and we error out
if it is missing), so we never regex-parse free text.
"""

import os
import anthropic

from .models import PatchPlan

SYSTEM_PROMPT = """You are an expert software engineer operating inside an \
automated self-healing CI pipeline. You will be given the output of a \
failing test run and the current contents of the source file(s) involved.

Diagnose the root cause and produce the SMALLEST correct fix. Respond only \
by calling the submit_patch_plan tool. Each patch is a search/replace edit:
  - file_path is the exact relative path given in the context
  - old is text copied verbatim from that file (same indentation), and must \
appear exactly once. Use the SMALLEST snippet that is unique -- ideally a \
single line next to where you want to change code. Never retype lines you \
are not changing; do not invent code that is not shown in the file.
  - new must differ from old in more than whitespace
  - new is the text that replaces old
  - keep edits minimal

Do not modify the test file unless the test itself is provably wrong.
"""


def format_context(c: dict) -> str:
    """One file block: AST focus snippets (most relevant code) first, then the full file."""
    focus = "".join(
        f"[Most relevant: {f['label']}, lines {f['start']}-{f['end']}]\n{f['source']}\n\n"
        for f in c.get("focus", [])
    )
    return f"--- FILE: {c['path']} ---\n{focus}[Full file]\n{c['content']}"


def generate_patch(
    test_output: str,
    file_contexts: list[dict],
    model: str = "claude-sonnet-5-5",
    feedback: str = "",
) -> PatchPlan:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)

    context_text = "\n\n".join(format_context(c) for c in file_contexts)

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
        tool_choice={"type": "auto"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Failing test output:\n```\n{test_output[-6000:]}\n```\n\n"
                    f"Relevant source files:\n{context_text}\n\n"
                    + (f"Earlier attempts that did not fix it:\n{feedback}\n\n" if feedback else "")
                    + "Diagnose and produce a patch."
                ),
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_patch_plan":
            return PatchPlan.model_validate(block.input)

    raise RuntimeError("Model did not return the expected submit_patch_plan tool call")
