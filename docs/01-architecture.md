# 01 - Architecture

## Why these pieces exist

**Settings object, not `os.environ`.** Every secret and knob flows through
`config.Settings`. Tests override one object; nodes stay pure.

**FRED client with disk cache.** Keyed by `(call, args, today)`. Reruns of a
debugging session are instant and deterministic for the day, FRED's rate limit
is never a concern, and fixtures for tests were recorded straight from the cache.
Errors are normalised to `FredError` with text a model can act on
("not found... use search_fred").

**Curated registry before search.** FRED has ~800k series. A 24-entry registry
with units and a one-line interpretation makes tool selection reliable and lets
tests assert on exact ids. `search_fred` is the escape hatch.

**Tools return text, not DataFrames.** Each tool output includes series id,
units, and observation dates so the model can cite them. Errors are *returned*
as strings starting with `ERROR:` rather than raised, so the ReAct loop feeds
them back to the model for self-correction (see `test_agent_recovers_from_tool_error`).

**Two graph entry points.** `graph` (no checkpointer) is what `langgraph dev`
loads: the server injects its own persistence. `build_graph(checkpointer)` is
what the CLI uses with `SqliteSaver`. Compiling with a custom checkpointer and
handing that to the server is a common footgun.

## Stage 1 graph

`langchain.agents.create_agent` produces a two-node loop:

```
__start__ -> model -> tools -> model -> ... -> __end__
```

State is `{"messages": [...]}` with the `add_messages` reducer. Each turn
appends Human -> AI(tool_calls) -> Tool -> ... -> AI(final).

## Testing strategy

- `tests/test_fred_client.py`: caching and error normalisation with a stub `fredapi`.
- `tests/test_tools.py`: formatting and partial-failure behaviour with recorded fixtures.
- `tests/test_graph.py`: a `GenericFakeChatModel` subclass with `bind_tools`
  returning scripted `AIMessage`s. Assert on the **trajectory** (which tools,
  which args, message count in the checkpoint), never on prose.

## Stage 2 graph (`graph_v2`, `build_graph_v2`)

```
START -> planner -> review -> fetch -> analyst -> critic -> finalize -> END
                      |  ^                 ^         |
                      |  '-- feedback      '-- reject (max 2)
                      '-- interrupt(): human approves or edits the plan
```

| Node | LLM? | In -> out |
|---|---|---|
| `planner` | yes, structured output (`Plan`) | question (+ feedback) -> `plan` |
| `review` | no | `interrupt()` with the plan; resume `"approve"` or feedback text -> `plan_feedback` |
| `fetch` | no | executes each step's tool deterministically -> `evidence` (errors recorded, not raised) |
| `analyst` | yes | evidence (+ critique) -> `draft` |
| `critic` | yes + regex | deterministic citation check + LLM fact-check -> `critique`, `revisions += 1` |
| `finalize` | no | `draft` -> `messages` (with a reviewer note if still rejected) |

Design decisions worth defending:
- **Plan is data.** Structured output turns "which tools" into something a
  human can approve or edit before any money/time is spent.
- **Fetch has no LLM.** Deterministic execution of an approved plan is cheap,
  testable, and never hallucinates.
- **Critic combines a regex check and an LLM check.** The regex catches
  citations of series/dates that were never fetched (free, exact). The LLM
  catches uncited numbers and historical claims (judgement). Both must pass.
- **Loop guard.** `revisions < MAX_REVISIONS` on the conditional edge, and
  `finalize` appends the unresolved issues rather than hiding them.
- **State holds plain dicts.** Pydantic models are validated inside nodes and
  stored as `model_dump()` so every checkpointer can serialise them.
- **Dynamic `interrupt()` over `interrupt_before`.** The payload tells the
  human what to decide, and `Command(resume=...)` carries their answer; works
  identically from the CLI, the REST API, and Studio.

Tests: `tests/test_graph_v2.py` scripts the three LLM factories and asserts on
routing (pause at review, re-plan on feedback, bounce on rejection, cap).

## Stage 3 additions

**Long-term memory (`memory.py`).** Checkpoints remember a thread; the Store
remembers the user. Feedback typed at plan review is written to the Store
namespace `("prefs", user)`; the planner reads it back on every new thread and
applies it when relevant. CLI: `/prefs`, `/forget`. In Studio the server
supplies the Store.

**Evals (`evals/`).** `cases.yaml` holds questions with expected series and
tools. `run_evals.py` runs each through the v2 graph with a fresh in-memory
checkpointer and store, scores `series_coverage`, `tools_ok`,
`citations_grounded` (regex) and `critic_approved`, writes
`results-<date>.json`, and with `--langsmith` uploads the dataset and runs an
experiment so prompt changes are comparable over time.

**Brief (`brief.py`).** `econ brief` runs a fixed three-question playbook
(labor, rates, growth/inflation) with auto-approved plans and writes
`outputs/brief-<date>.md` with embedded charts. Deterministic questions make it
schedulable and comparable day to day.

## Threads and per-thread configuration

Everything about "which conversation" and "which user" is carried in
`config["configurable"]`, not in state or code:

| key | selects | set by |
|---|---|---|
| `thread_id` | the checkpoint lineage (conversation) | `--thread NAME`, `--last`, `/thread ...` |
| `user_id` | the long-term memory namespace `("prefs", user_id)` | `--user NAME` |
| `checkpoint_id` | a specific past checkpoint to resume/fork from | `/replay N` |

`threads.py` adds what the checkpointer API leaves out: list (newest first,
with first question and message count), rename (`copy_thread` + delete) and
delete. Thread ids are free-form strings, so readable names are the convention.
Nodes read `user_id` with `langgraph.config.get_config()`, which works for the
CLI, the REST API and Studio alike.

## Additions on 2026-10-13 (the "build everything" pass)

**Eighteen tools.** The original eight plus ALFRED vintages (`series_as_of`, `revision_history`,
`release_calendar`) and analytics (`extremes`, `percentile_rank`, `compare_to_prepandemic`,
`sahm_rule`, `recession_probability`, `correlate`, `macro_snapshot`). `extremes` exists because
the window summary gives a max without its date; the planner is told to call it whenever a
peak or low is mentioned.

**Argument validation in the planner.** Structured output fixes the shape of a `Plan`, not the
contents of each step's `args`. `_validate_steps` checks them against the tool's `args_schema`;
the planner retries once with the errors, then drops anything still invalid.

**Prior evidence.** `prior_evidence` in state carries the previous turns' evidence (capped,
marked `prior=True`) so follow-ups cite real data instead of the previous answer's prose. The
analyst sees it as evidence; the critic checks against it.

**Numeric grounding in the critic.** `_numeric_checks` requires every number in the draft to
be in the evidence (directly or scaled by 10^3 / 10^6), or derivable as a pairwise difference
or percent change within an evidence block. Years and small unitless counts are exempt;
"7,437,970 thousand" is flagged as a unit error. Calibrated to zero false positives on twenty
recorded drafts; measured at precision 1.0 / recall 1.0 on `evals/critic_cases.yaml`.

**Concurrent fetch, retries, staleness.** Steps run in a small thread pool; the FRED client
retries transient failures with backoff and flags series whose last observation is older than
their frequency suggests.

**Editable plans.** `review` accepts `{"plan": {...}}` on resume; the web UI edits steps inline.

**Persistence factory.** `persistence.open_persistence` returns SQLite or Postgres
checkpointer + store from `DATABASE_URL`; verified against Postgres 16 in Docker.

**Passthrough hardening.** The web API route allows only the endpoints the desk uses and
rate-limits per IP, so a public deployment exposes a narrow surface.

## Terminal-grade interface (web)

The desk takes its interaction model from a trading terminal rather than a chat app:

- `CommandBar.tsx`: one command line for questions, series mnemonics and slash commands, with
  registry-driven suggestions, history, and `⌘K` / `/` focus from anywhere.
- `SeriesCard.tsx` + `/api/series/[id]`: a series at a glance, served from FRED server-side.
- `StatusBar.tsx`: mode, thread, user, last run time, key hints; always visible.
- Watchlist tiles (`/api/fred?ids=`) persisted per browser; a right-hand monitor pane on wide
  screens so charts and sources stay in view; evidence rendered as numeric tables
  (`lib/evidenceTable.ts`); `Enter`/`E`/`Esc` shortcuts handled at the window level, never
  inside inputs.

## Monitor page (2026-10-23)

`/monitor` is a second screen for the desk: `web/src/app/api/monitor/route.ts` builds the Treasury
curve (11 maturities, latest common date vs a year earlier), the Sahm rule and curve-probit
gauges, and 51 state unemployment series from FRED with a small concurrency pool and hourly
caching, sharing its arithmetic with `web/src/lib/monitor.ts` (unit-tested against the Python
tools' numbers). Without a server-side key it serves `web/src/demo/monitor.json`, recorded by
`scripts/record_monitor.py`. The agent gained the matching tools, `term_structure` and
`regional_compare`, and FRED-side transforms on `get_series`.

