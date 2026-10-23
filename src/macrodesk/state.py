"""Graph state for the Stage 2 hand-built graph.

Why explicit state instead of just `messages`?
- The planner's output is *data* (which tools, which args), so it can be shown
  to a human for approval, edited, and executed deterministically.
- Evidence is kept separately from chat history so the critic can check the
  draft against exactly what was fetched, nothing else.
- revisions is a loop guard: the critic can only bounce a draft so many times.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

ToolName = Literal[
    "list_known_series", "search_fred", "get_series", "compute_change",
    "compare_series", "yield_curve_snapshot", "recession_periods", "plot_series",
    "series_as_of", "revision_history", "release_calendar",
    "extremes", "percentile_rank", "compare_to_prepandemic", "sahm_rule", "recession_probability", "correlate",
    "macro_snapshot", "term_structure", "regional_compare",
]


class Step(BaseModel):
    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)
    why: str = Field(description="One line on what this step will establish.")


class Plan(BaseModel):
    goal: str = Field(description="Restate the user's question in one sentence.")
    steps: list[Step] = Field(min_length=1, max_length=8)


class Evidence(BaseModel):
    tool: str
    args: dict[str, Any]
    result: str
    ok: bool


class Critique(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list,
                              description="Each issue: the offending sentence and why it is unsupported.")


class AgentState(TypedDict, total=False):
    """Checkpointed state. Pydantic models above are validated *inside* nodes;
    the state itself holds their `.model_dump()` dicts so every checkpointer
    (SQLite, Studio's in-memory server, Postgres) can serialise it without
    registering custom types (see debugging note 9)."""

    messages: Annotated[list[AnyMessage], add_messages]
    plan: dict[str, Any] | None
    plan_feedback: str | None       # set by review when the human asks for changes
    evidence: list[dict[str, Any]]
    prior_evidence: list[dict[str, Any]]   # evidence from earlier questions in this thread (marked prior=True)
    draft: str
    critique: dict[str, Any] | None
    revisions: int
    charts: list[dict[str, Any]]   # JSON chart payloads for web clients (one per plot_series step)
