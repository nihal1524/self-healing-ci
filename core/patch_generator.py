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
from .observability import observe

MAX_TOOL_ATTEMPTS = 3

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

    prompt = (
        f"Failing test output:\n```\n{test_output[-6000:]}\n```\n\n"
        f"Relevant source files:\n{context_text}\n\n"
        + (f"Earlier attempts that did not fix it:\n{feedback}\n\n" if feedback else "")
        + "Diagnose and produce a patch."
    )

    problem = ""
    for attempt in range(1, MAX_TOOL_ATTEMPTS + 1):
        # Metadata only: the (large) prompt is not recorded, the model's edits are.
        with observe(
            "patch-generation",
            as_type="generation",
            model=model,
            metadata={"attempt": attempt, "prompt_chars": len(prompt + problem), "context_files": len(file_contexts)},
        ) as generation:
            message = client.messages.create(
                model=model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": prompt + problem}],
            )
            call = next(
                (b for b in message.content if b.type == "tool_use" and b.name == "submit_patch_plan"),
                None,
            )
            usage = getattr(message, "usage", None)
            generation.update(
                output=call.input if call else None,
                usage_details={"input": usage.input_tokens, "output": usage.output_tokens} if usage else None,
                metadata={"stop_reason": getattr(message, "stop_reason", None), "valid_tool_call": call is not None},
            )
        try:
            if call is None:
                raise ValueError("no submit_patch_plan tool call was returned")
            return PatchPlan.model_validate(call.input)
        except ValueError as exc:  # pydantic.ValidationError is a ValueError
            print(f"    Invalid model response (try {attempt}/{MAX_TOOL_ATTEMPTS}): {str(exc).splitlines()[0]}")
            problem = (
                f"\n\nYour previous response was invalid: {exc}\n"
                "Call submit_patch_plan again with `patches` as a JSON list of objects."
            )

    raise RuntimeError(f"Model did not return a valid submit_patch_plan call after {MAX_TOOL_ATTEMPTS} tries")
