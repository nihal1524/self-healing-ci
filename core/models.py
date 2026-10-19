"""
Pydantic models that define the *structured output* contract between the LLM
and the rest of the pipeline. We hand this schema to Claude as a tool
definition and force a tool_use response, so we always get back valid,
typed data instead of having to regex-parse free text.
"""

from typing import List
from pydantic import BaseModel, Field


class ProposedPatch(BaseModel):
    file_path: str = Field(
        ...,
        description="Path to the file to patch, relative to the repo root (e.g. 'src/calculator.py').",
    )
    explanation: str = Field(
        ...,
        description="Short explanation of why this change fixes the failing test.",
    )
    old: str = Field(
        ...,
        description=(
            "Exact text to replace, copied verbatim from the file (including "
            "indentation). It must appear exactly once in the file."
        ),
    )
    new: str = Field(..., description="Text that replaces `old`.")


class PatchPlan(BaseModel):
    root_cause: str = Field(
        ..., description="One or two sentence diagnosis of why the test is failing."
    )
    patches: List[ProposedPatch] = Field(
        ..., description="One or more file patches that together fix the failing test."
    )
