"""Node functions for the Stage 2 graph.

Each node is a plain function: state in, partial state out. LLM access goes
through small factory functions (`planner_llm`, `analyst_llm`, `critic_llm`) so
tests can monkeypatch them with scripted stand-ins.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from macrodesk.models import make_model
from macrodesk.logging import log_event
from macrodesk.memory import current_user_id, recall_feedback, remember_feedback
from macrodesk.state import AgentState, Critique, Evidence, Plan
from macrodesk.tools import ALL_TOOLS, plot_series_payload

TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}
MAX_REVISIONS = 2


def _tool_catalog() -> str:
    """Name, argument names and the first line of each description: enough to plan, cheap in tokens."""
    lines = []
    for t in ALL_TOOLS:
        args = ", ".join((t.args_schema.model_json_schema().get("properties") or {}).keys()) if t.args_schema else ""
        lines.append(f"- {t.name}({args}): {t.description.strip().splitlines()[0]}")
    return "\n".join(lines)


def _validate_steps(plan: Plan) -> list[str]:
    """Check each step's args against the tool's input schema so fetch never runs an impossible call."""
    problems = []
    for i, st in enumerate(plan.steps, 1):
        tool = TOOLS_BY_NAME.get(st.tool)
        if tool is None:
            problems.append(f"step {i}: unknown tool {st.tool!r}")
            continue
        if tool.args_schema is not None:
            try:
                tool.args_schema.model_validate(st.args)
            except Exception as e:  # pydantic ValidationError
                first = str(e).splitlines()[0:3]
                problems.append(f"step {i} ({st.tool}): invalid arguments {st.args}: {' '.join(first)[:200]}")
    return problems


def _recent_turns(state: AgentState, n: int = 2, max_chars: int = 500) -> str:
    """Earlier question/answer pairs in this thread, so follow-ups plan against what was already said."""
    msgs = state.get("messages", [])
    pairs: list[tuple[str, str]] = []
    q = None
    for m in msgs[:-1]:  # exclude the current question
        if isinstance(m, HumanMessage):
            q = m.text if isinstance(m.text, str) else str(m.content)
        elif isinstance(m, AIMessage) and q is not None:
            a = m.text if isinstance(m.text, str) else str(m.content)
            pairs.append((q, a)); q = None
    if not pairs:
        return ""
    out = ["Earlier in this conversation (for context; fetch fresh evidence for anything you state):"]
    for q, a in pairs[-n:]:
        out.append(f"Q: {q[:max_chars]}\nA: {a[:max_chars]}{'…' if len(a) > max_chars else ''}")
    return "\n".join(out)


def _last_user_text(state: AgentState) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return m.text if isinstance(m.text, str) else str(m.content)
    return ""


# ---- LLM factories (monkeypatched in tests) ----------------------------------
def planner_llm():
    return make_model("planner").with_structured_output(Plan)


def analyst_llm():
    return make_model()


def critic_llm():
    return make_model("critic").with_structured_output(Critique)


# ---- nodes --------------------------------------------------------------------
PLANNER_SYSTEM = """You plan data retrieval for an economic analyst. Produce a short, concrete plan of tool calls
that will gather ALL the evidence needed to answer the question. Rules:
- Use series ids from the curated list when possible (JOLTS: JTSJOL JTSHIR JTSQUR JTSLDL; UNRATE PAYEMS ICSA;
  GDPC1 A191RL1Q225SBEA INDPRO; CPIAUCSL CPILFESL PCEPILFE; DGS3MO DGS2 DGS10 DGS30 T10Y2Y T10Y3M FEDFUNDS;
  UMCSENT HOUST RSAFS). Use search_fred only for something outside that list.
- If the user wants history or context (peaks, pre-pandemic levels), fetch a long enough window (start=YYYY-MM-DD)
  so that the analyst never has to rely on memory.
- Prefer compute_change for growth rates and yield_curve_snapshot for curve questions.
- Any mention of a peak, trough, high or low needs `extremes` so the DATE can be cited.
- "What was known at the time" -> series_as_of; "how much was it revised" -> revision_history;
  "when is the next release" -> release_calendar; "is this high historically" -> percentile_rank;
  "back to normal / vs 2019" -> compare_to_prepandemic; recession risk -> sahm_rule and recession_probability;
  "state of the economy" -> macro_snapshot; "do X and Y move together" -> correlate;
  curve shape / steepening / flattening -> term_structure; states or regions -> regional_compare.
- For year-over-year rates or quarterly averages, pass FRED transforms to get_series
  (units="pc1", or frequency="q" with aggregation="avg") instead of computing them.
- Include plot_series only if the user asks for a chart/graph/plot.
- Every step must include every required argument (series_id for most tools). Never emit empty args for a tool
  that needs a series.
- For a follow-up question, plan only what the follow-up needs. Evidence fetched earlier in this thread stays
  available to the writer, so do not re-fetch it unless the question needs a different window or series.
{prefs}
{history}
Available tools:
{catalog}"""


def planner(state: AgentState, runtime: Runtime) -> dict[str, Any]:
    question = _last_user_text(state)
    feedback = state.get("plan_feedback")
    user_id = current_user_id()
    remembered = recall_feedback(runtime.store, user_id)
    prefs = ""
    if remembered:
        prefs = ("Feedback this user gave on earlier plans (apply when relevant to this question):\n"
                 + "\n".join(f"- {r}" for r in remembered))
    log_event("planner.recall", user_id=user_id, n_prefs=len(remembered))
    prompt = [SystemMessage(PLANNER_SYSTEM.format(catalog=_tool_catalog(), prefs=prefs, history=_recent_turns(state))),
              HumanMessage(question)]
    if feedback and state.get("plan"):
        prompt.append(AIMessage(f"Previous plan: {Plan.model_validate(state['plan']).model_dump_json()}"))
        prompt.append(HumanMessage(f"Revise the plan with this feedback: {feedback}"))
    llm = planner_llm()
    plan: Plan = llm.invoke(prompt)
    problems = _validate_steps(plan)
    if problems:
        log_event("planner.invalid_args", problems=problems)
        prompt = prompt + [AIMessage(plan.model_dump_json()),
                           HumanMessage("Fix these problems in the plan and return the corrected plan:\n" + "\n".join(problems))]
        plan = llm.invoke(prompt)
        problems = _validate_steps(plan)
        if problems:  # still broken: drop the invalid steps rather than run tools that will error
            bad = {int(p.split()[1]) for p in problems if p.startswith("step ")}
            plan.steps = [st for i, st in enumerate(plan.steps, 1) if i not in bad] or plan.steps
            log_event("planner.dropped_invalid_steps", dropped=sorted(bad))
    log_event("planner.done", n_steps=len(plan.steps), tools=[s.tool for s in plan.steps], feedback=feedback)
    # Keep earlier turns' evidence available (marked prior=True, newest last, capped) so follow-ups can cite it
    # without re-fetching. A re-plan after review feedback keeps what it already had.
    if feedback:
        prior = state.get("prior_evidence") or []
    else:
        prior = [dict(e, prior=True) for e in (state.get("prior_evidence") or []) + (state.get("evidence") or [])][-12:]
    return {"plan": plan.model_dump(), "plan_feedback": None, "evidence": [], "charts": [], "draft": "",
            "critique": None, "revisions": 0, "prior_evidence": prior}


def review(state: AgentState, runtime: Runtime) -> dict[str, Any]:
    """Human-in-the-loop. Pauses the graph; resume with Command(resume='approve') or free text feedback.
    Feedback is also written to long-term memory so future plans start from it."""
    plan = Plan.model_validate(state["plan"])
    decision = interrupt({
        "type": "plan_review",
        "goal": plan.goal,
        "steps": [s.model_dump() for s in plan.steps],
        "hint": "resume with 'approve' or with feedback text to replan",
    })
    if isinstance(decision, dict) and "plan" in decision:
        # An edited plan from the UI: validate it and run it as approved.
        edited = Plan.model_validate(decision["plan"])
        log_event("review.edited", n_steps=len(edited.steps))
        return {"plan": edited.model_dump(), "plan_feedback": None}
    if isinstance(decision, str) and decision.strip().lower() in ("", "approve", "y", "yes", "ok"):
        log_event("review.approved")
        return {"plan_feedback": None}
    log_event("review.feedback", feedback=decision)
    remember_feedback(runtime.store, str(decision), current_user_id())
    return {"plan_feedback": str(decision)}


def route_after_review(state: AgentState) -> str:
    return "planner" if state.get("plan_feedback") else "fetch"


def fetch(state: AgentState) -> dict[str, Any]:
    """Deterministic: execute the approved plan. No LLM involved. Steps run concurrently
    (FRED calls are I/O bound); results are recorded in plan order."""
    steps = Plan.model_validate(state["plan"]).steps

    def run(step):
        try:
            result = TOOLS_BY_NAME[step.tool].invoke(step.args)
            return str(result), not str(result).startswith("ERROR")
        except Exception as e:  # bad args etc. -> keep going, record it
            return f"ERROR: {type(e).__name__}: {e}", False

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(steps)))) as pool:
        results = list(pool.map(run, steps))

    evidence: list[dict[str, Any]] = []
    charts: list[dict[str, Any]] = []
    for step, (result, ok) in zip(steps, results):
        evidence.append(Evidence(tool=step.tool, args=step.args, result=str(result), ok=ok).model_dump())
        if step.tool == "plot_series" and ok:
            try:
                payload = plot_series_payload(**step.args)
                if payload:
                    charts.append(payload)
            except Exception as e:  # chart data is a nice-to-have for web clients
                log_event("fetch.chart_payload_failed", error=str(e))
        log_event("fetch.step", tool=step.tool, args=step.args, ok=ok, chars=len(str(result)))
    return {"evidence": evidence, "charts": charts}


ANALYST_SYSTEM = """You are an economic analyst. Write the answer to the user's question using ONLY the evidence
below. Hard rules:
- Every number, date, peak, average or comparison you state must appear in the evidence. If the evidence does not
  contain it, do not say it - say that it was not retrieved instead.
- Cite each figure as (SERIES_ID, YYYY-MM-DD). State units.
- Units are already applied in the evidence: a value of 7,437.97 in a series whose units are "Thousands" means
  7,437,970 (about 7.4 million). Write it as "7,438 thousand" or "about 7.4 million", never "7,437,970 thousand".
- Lead with the answer, then 3-6 evidence bullets, then caveats (release lags, revisions).
- If a chart was produced, say so in one short line; the chart itself is shown to the user separately. Never include file paths.
{critique_block}
EVIDENCE:
{evidence}"""


def _all_evidence(state: AgentState) -> list[dict[str, Any]]:
    """Current evidence plus evidence carried over from earlier turns in the thread."""
    return list(state.get("evidence") or []) + list(state.get("prior_evidence") or [])


def _format_evidence(ev: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"[{i}] {'(from an earlier question in this thread) ' if e.get('prior') else ''}{e['tool']}({e['args']})\n{e['result']}"
                        for i, e in enumerate(ev, 1))


def analyst(state: AgentState) -> dict[str, Any]:
    critique = Critique.model_validate(state["critique"]) if state.get("critique") else None
    critique_block = ""
    if critique and not critique.approved:
        critique_block = ("A reviewer rejected your previous draft. Fix these issues (remove or re-source each claim):\n"
                          + "\n".join(f"- {i}" for i in critique.issues)
                          + f"\n\nPREVIOUS DRAFT:\n{state.get('draft', '')}\n")
    history = _recent_turns(state, n=1, max_chars=700)
    prompt = [SystemMessage(ANALYST_SYSTEM.format(critique_block=critique_block,
                                                  evidence=_format_evidence(_all_evidence(state)))
                            + (f"\n\n{history}" if history else "")),
              HumanMessage(_last_user_text(state))]
    # Stream the draft token by token as custom events so clients can render it as it is written.
    # (The checkpointed `draft` is still set once, below, when the node returns.)
    try:
        writer = get_stream_writer()
    except Exception:  # not inside a graph run (unit tests calling the node directly)
        writer = lambda _: None  # noqa: E731
    writer({"draft_reset": True})
    parts: list[str] = []
    for chunk in analyst_llm().stream(prompt):
        text = chunk.text if isinstance(getattr(chunk, "text", None), str) else ""
        if text:
            parts.append(text)
            writer({"draft_delta": text})
    draft = "".join(parts)
    log_event("analyst.draft", chars=len(draft), revision=state.get("revisions", 0))
    return {"draft": draft}


CRITIC_SYSTEM = """You are a strict but fair fact-checker. Compare the DRAFT against the EVIDENCE.

FLAG a sentence only if it states a number, date, level, peak, average, range, or historical comparison that is
neither present in the evidence nor derivable from it by simple arithmetic.
  - Unsupported: "peaked near 12 million in 2022" when no 2022 data was fetched; a date or value that appears
    nowhere; a "series low" claim when the evidence shows a lower value; a unit error such as reporting a value
    given in thousands as if it were units (7,437.97 thousand is NOT 7,437,970 thousand).
  - Supported (do NOT flag): differences, percentage-point gaps vs a stated target, counts like "5 of the 6
    readings" or "five of the six readings" that you can verify by counting the listed values, ranges whose ends
    are listed values, min/max/mean quoted from the evidence's summary line, a percentage computed from two
    evidence values (e.g. "42% below the 12,301 maximum" when both numbers are present), and qualitative
    interpretation. A maximum or minimum described as "in the window shown" or with a note that the exact date
    was not retrieved is supported by the summary line.
When the evidence shows a window (e.g. "latest 6") treat "peak"/"low" claims as supported if the draft says
"in the window shown" or similar; flag them only if stated as all-time without qualification.

Output rules: `issues` must contain ONLY genuine problems, each quoting the offending sentence and saying what
the evidence actually shows. Never include an entry that says there is no issue. If `issues` is empty,
`approved` must be true; if it is non-empty, `approved` must be false. Double-check your own arithmetic
before flagging a number."""

CRITIC_USER = """EVIDENCE:
{evidence}

DRAFT:
{draft}"""

_CITE_RE = re.compile(r"\(([A-Z0-9]{3,20}),\s*(\d{4}-\d{2}-\d{2})\)")


def _deterministic_checks(draft: str, evidence: list[dict[str, Any]]) -> list[str]:
    """Cheap, non-LLM checks: cited series ids must appear in fetched evidence."""
    blob = "\n".join(e["result"] for e in evidence)
    issues = []
    for sid, d in set(_CITE_RE.findall(draft)):
        if sid not in blob:
            issues.append(f"Cites ({sid}, {d}) but no evidence for series {sid} was fetched.")
        elif d not in blob:
            issues.append(f"Cites ({sid}, {d}) but date {d} does not appear in the evidence.")
    return issues


_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SERIES_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*\d[A-Z0-9]*\b")
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_APPROX_RE = re.compile(r"(about|roughly|around|nearly|approximately|almost|~|≈|close to|just under|just over)\s*$", re.I)
_UNIT_AFTER_RE = re.compile(r"^\s*(thousand|million|billion|k\b|m\b|bn\b|%|pp\b|percentage points?|bps?\b)", re.I)


_CONSTANTS = {2.0, 0.5, 100.0, 300.0}  # Fed target, Sahm threshold, payroll breakeven (k), claims warning level (k)


def _numbers_in(text: str) -> list[float]:
    clean = _SERIES_RE.sub(" ", _DATE_RE.sub(" ", text))
    return [abs(float(m.group().replace(",", ""))) for m in _NUMBER_RE.finditer(clean)]


def _derived(nums: list[float]) -> tuple[set[float], set[float]]:
    """Pairwise absolute differences and percent changes within one evidence block (rounded)."""
    nums = nums[:150]
    diffs, pcts = set(), set()
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            if a != b:  # a zero difference is not a derivation, and it would match tiny scaled values
                diffs.add(round(abs(a - b), 3))
            diffs.add(round(a + b, 3))  # "prior quarter = latest minus a negative change" is a sum
            if b:
                pcts.add(round(abs(a / b - 1) * 100, 2))
            if a:
                pcts.add(round(abs(b / a - 1) * 100, 2))
    return diffs, pcts


def _close(v: float, pool, tol_rel: float, tol_abs: float = 0.011) -> bool:
    # the absolute tolerance covers two-decimal rounding; it never applies against a zero
    return any(abs(v - e) <= (max(tol_abs, tol_rel * abs(e)) if e else 0.0) for e in pool)


def _numeric_checks(draft: str, evidence: list[dict[str, Any]], question: str = "") -> list[str]:
    """Deterministic number grounding: every number in the draft must appear in the evidence,
    possibly scaled by 1,000 or 1,000,000, or be derivable as a difference or percent change
    between two evidence numbers. Years and small counts are exempt. Flags the classic unit error
    of writing a value that is already in thousands as "N thousand" after multiplying it out."""
    ev_nums: list[float] = []
    diffs: set[float] = set(); pcts: set[float] = set()
    for e in evidence:
        nums = _numbers_in(e["result"])
        ev_nums += nums
        d, p = _derived(nums)
        diffs |= d; pcts |= p
    # numbers the user introduced are fair to repeat, plus a few named policy constants
    allowed = set(_numbers_in(question)) | _CONSTANTS
    ev_pool = set(ev_nums)
    pool = ev_pool | allowed
    issues: list[str] = []
    clean = _SERIES_RE.sub(" ", _DATE_RE.sub(" ", draft))
    seen: set[str] = set()
    for m in _NUMBER_RE.finditer(clean):
        raw = m.group()
        v = abs(float(raw.replace(",", "")))
        is_int = "." not in raw and "," not in raw
        before = clean[max(0, m.start() - 16):m.start()]
        after = clean[m.end():m.end() + 20]
        um = _UNIT_AFTER_RE.match(after)
        unit = um.group(1).strip().lower() if um else ""
        # small counts ("5 of 6 readings", "12-month") and years are exempt unless a unit follows them
        if raw in seen or (is_int and not unit and (v <= 60 or 1900 <= v <= 2100)):
            continue
        seen.add(raw)
        tol = 0.05 if _APPROX_RE.search(before) else 0.012
        scale_unit = unit in ("thousand", "million", "billion", "k", "m", "bn")
        if _close(v, ev_pool if scale_unit else pool, tol):
            continue
        scaled_up = v >= 100 and _close(v / 1000, ev_pool, tol)   # draft wrote 7,437,970 for an evidence value of 7,437.97
        scaled_down = v < 10000 and (_close(v * 1000, ev_pool, tol) or _close(v * 1e6, ev_pool, tol))
        if scaled_up and unit == "thousand":
            issues.append(f"Unit error: '{raw} thousand' multiplies a value that the evidence already gives in thousands.")
            continue
        if scaled_up or scaled_down:
            continue
        dtol = max(tol, 0.02)
        scaled = [v] + ([v / 1000] if v >= 100 else []) + ([v * 1000] if v < 10000 else [])
        if any(_close(x, diffs, dtol) for x in scaled) or _close(v, pcts, dtol, tol_abs=0.06):
            continue
        issues.append(f"The number {raw}{(' ' + unit) if unit else ''} does not appear in the evidence and is not a difference "
                      f"or percent change between evidence values.")
    return issues[:6]


def _verified_numbers(draft: str, evidence: list[dict[str, Any]], question: str) -> set[str]:
    """Raw number tokens in the draft that the deterministic layer accepts."""
    flagged = {m.group(1) for i in _numeric_checks(draft, evidence, question) for m in [re.search(r"number ([-+\d.,]+)|'([-+\d.,]+) thousand'", i)] if m}
    clean = _SERIES_RE.sub(" ", _DATE_RE.sub(" ", draft))
    return {m.group() for m in _NUMBER_RE.finditer(clean)} - flagged


_QUOTED_RE = re.compile(r'[“"]([^”"]{8,}?)[”"]')
_SELF_RESOLVED_RE = re.compile(r"\b(no issue|not an issue|is correct|is supportable|is reasonable and supportable)\b", re.I)


def _overrule(llm_issues: list[str], verified: set[str]) -> tuple[list[str], list[str]]:
    """Drop model objections that are only about numbers the deterministic layer verified.
    The model layer keeps authority over prose claims (trends, causes, uncited history); the
    deterministic layer keeps authority over numbers it can check exactly. Only the sentence the
    critic quotes from the draft is examined, not the numbers the critic introduces while
    explaining; an objection that ends by saying there is no issue is dropped as self-resolved."""
    kept, overruled = [], []
    for issue in llm_issues:
        if _SELF_RESOLVED_RE.search(issue):
            overruled.append(issue)
            continue
        quoted = _QUOTED_RE.search(issue)
        target = quoted.group(1) if quoted else issue
        nums = [m.group() for m in _NUMBER_RE.finditer(_SERIES_RE.sub(" ", _DATE_RE.sub(" ", target)))]
        nums = [x for x in nums if not (("." not in x and "," not in x) and (float(x) <= 60 or 1900 <= float(x) <= 2100))]
        if nums and all(x in verified for x in nums):
            overruled.append(issue)
        else:
            kept.append(issue)
    return kept, overruled


def critic(state: AgentState) -> dict[str, Any]:
    evidence = _all_evidence(state)
    issues = _deterministic_checks(state["draft"], evidence)
    issues += _numeric_checks(state["draft"], evidence, _last_user_text(state))
    # Anthropic requires at least one non-system message (see debugging note 6).
    verdict: Critique = critic_llm().invoke([
        SystemMessage(CRITIC_SYSTEM),
        HumanMessage(CRITIC_USER.format(evidence=_format_evidence(evidence), draft=state["draft"])),
    ])
    llm_kept, overruled = _overrule(verdict.issues, _verified_numbers(state["draft"], evidence, _last_user_text(state)))
    issues += llm_kept
    approved = not issues  # the model's `approved` is informative; the issue list is the gate
    log_event("critic.verdict", approved=approved, n_issues=len(issues), issues=issues,
              deterministic=len(issues) - len(llm_kept), overruled=overruled)
    return {"critique": Critique(approved=approved, issues=issues).model_dump(),
            "revisions": state.get("revisions", 0) + 1}


def route_after_critic(state: AgentState) -> str:
    c = state.get("critique")
    if c and not c["approved"] and state.get("revisions", 0) < MAX_REVISIONS:
        return "analyst"
    return "finalize"


def finalize(state: AgentState) -> dict[str, Any]:
    text = state["draft"]
    c = state.get("critique")
    if c and not c["approved"]:
        text += ("\n\n_Reviewer note: this answer still had unresolved issues after "
                 f"{state.get('revisions', 0)} revisions: " + "; ".join(c["issues"]) + "_")
    return {"messages": [AIMessage(text)]}
