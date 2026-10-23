# 02 - Debugging notes (running log)

Each entry: what broke, how it was found, what fixed it. These are the
interview stories.

## 1. `uv add` failed: hatch could not find the package
**Symptom.** `uv add ...` printed a hatch hint about `packages = ["src/foo"]` and
nothing installed.
**Cause.** I deleted `src/macrodesk` after `uv init --lib`, so the build backend
had nothing to build when resolving the project itself.
**Fix.** Explicit `[tool.hatch.build.targets.wheel] packages = ["src/macrodesk"]`
and recreate the package dir before syncing.
**Lesson.** uv builds *your* project during sync; an unbuildable project blocks
every dependency.

## 2. `cryptography` tried to compile Rust
**Symptom.** `uv sync` spent minutes downloading rustup and failed on `xcrun`.
**Cause.** `langgraph-cli[inmem]` -> `langgraph-api` -> `cryptography==50`, which
has no wheel for this macOS/Python combination.
**Fix.** `[tool.uv] constraint-dependencies = ["cryptography<46"]`.
**Lesson.** Transitive deps can drag in native builds; constraints are cheaper
than toolchains.

## 3. 404 `DeploymentNotFound` for `claude-opus-5-5`
**Symptom.** First real agent run raised `AnthropicModelNotFoundError` from
inside the `model` node (LangGraph surfaces the node name in the traceback:
`During task with name 'model'`).
**Cause.** The shell exports `ANTHROPIC_BASE_URL` pointing at a Microsoft
Foundry gateway. The Anthropic SDK honours that silently, and Foundry addresses
models by *deployment name*; the only deployment is `claude-sonnet-4-6`.
**Fix.** `Settings.model` now falls back to `ANTHROPIC_MODEL` when set.
**Lesson.** When an LLM call fails, check the SDK's environment-driven config
(`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`) before blaming the code. Print the
host, never the key.

## 4. Model hallucinated history it never fetched
**Symptom.** In the first successful JOLTS answer the model said quits peaked
"~3.0% in late 2021" and "averaged 2.1-2.2% in 2018-2019". It only fetched data
from 2023-10 onward. The numbers happen to be roughly right, but they are from
training memory, not from FRED, and were not cited per the system prompt.
**How found.** Reading the tool-result panels against the prose. The JSONL log
confirms only two `fred.call` events.
**Status.** Open. Prompt tweaks help a little; the real fix is Stage 2's critic
node that rejects any number lacking a `(SERIES_ID, date)` citation backed by
`evidence` in state.
**Lesson.** "Grounded" is a property you verify, not a prompt you write.

## 5. Streaming loop ran the graph twice
**Symptom.** A hastily written `for node, update in graph.stream(...).__iter__() and (...)`
expression would have invoked the graph twice per turn.
**How found.** Re-reading the code before running it.
**Fix.** Plain nested loop over `graph.stream(..., stream_mode="updates")`.
**Lesson.** `stream()` is lazy; every iteration of a new generator is a new run.

## 6. Studio run failed: "messages: at least one message is required"
**Symptom.** First run from LangGraph Studio errored inside the `model` node
with an Anthropic 400, preceded by `UserWarning: Received only system message(s)`.
**Cause.** The run was submitted with an empty `messages` list. Studio's input
form starts empty; Submit without adding a message sends `{"messages": []}`,
so the only thing reaching the API was the system prompt.
**How found.** The warning names the exact shape of the request. Reproduced with
`curl POST /runs/wait` using `{"messages": []}` (fails) vs one user message (works).
**Fix.** In Studio, click **+ Message**, type the question, then Submit. No code
change needed; a planner node in Stage 2 will validate input explicitly.
**Lesson.** Read the *warning* just above the traceback. The provider SDK often
tells you what it sent before the server tells you why it refused.

## 7. Circular import between graph.py and nodes.py
**Symptom.** `AttributeError: partially initialized module 'macrodesk.nodes' has no attribute 'planner'`
at test collection, right after adding Stage 2.
**Cause.** `graph.py` imports `nodes` to build the graph; `nodes.py` imported
`make_model` from `graph.py`. Python executes `graph.py` top to bottom, hits
`import nodes`, which re-enters `graph.py` before `make_model` exists.
**Fix.** Move `make_model` into `models.py`, which neither module depends on.
**Lesson.** Module-level `graph = build_graph()` is convenient for Studio but
makes import order load-bearing. Keep factories in leaf modules.

## 8. My own critic node hit lesson 6
**Symptom.** Stage 2 run died in the `critic` node with the same 400
"messages: at least one message is required".
**Cause.** I had put evidence and draft inside one `SystemMessage` and sent no
human turn. Exactly the shape I had documented two hours earlier.
**Fix.** Instructions in the system message; evidence + draft as the human turn.
**Lesson.** Write a test that exercises the real prompt shape against a model
stub that *validates* message roles, not just one that returns canned output.

## 9. "Deserializing unregistered type macrodesk.state.Plan from checkpoint"
**Symptom.** Warning on every `get_state` once Stage 2 stored Pydantic objects
in graph state; LangGraph says this will become an error.
**Cause.** The checkpointer serialises state with msgpack and only round-trips
arbitrary classes via a registry. Custom types in state are a liability for
Studio's server checkpointer and for Postgres later.
**Fix.** State fields are plain dicts; nodes do `Plan.model_validate(...)` on
the way in and `.model_dump()` on the way out.
**Lesson.** Graph state is a wire format. Keep it JSON-shaped.

## 10. A false alarm, and why it was worth chasing
**Symptom.** A Stage 2 answer cited `(JTSLDL, 2022-09-01)` although I believed
the plan had fetched only 36 recent observations.
**How checked.** Loaded the thread's checkpoint, printed each evidence block,
grepped the dates, and re-ran `_deterministic_checks` on the stored draft.
**Finding.** This run's planner had asked for 48 observations; the dates were
in the evidence and the critic was right to approve.
**Lesson.** Inspect state before blaming the model. The checkpoint made the
verification a 20-line script instead of a re-run.

## 11. `cannot start a transaction within a transaction` from the Store
**Symptom.** `econ brief` died in the `planner` node with a sqlite3
OperationalError the first time a node read the Store. Unit tests were green.
**Cause.** `SqliteSaver` and `SqliteStore` were built on the same
`sqlite3.Connection`. The checkpointer holds a transaction open around a step;
the store then issues its own `BEGIN`. The tests used `InMemoryStore`, so they
never exercised the real pairing.
**Fix.** Give the store its own connection to the same file.
**Lesson.** Two libraries managing transactions must not share a connection.
And a test double that is *too* convenient can hide an integration bug;
the eval run caught it, the unit tests could not.

## 12. The critic rejects correct arithmetic (and sometimes miscalculates)
**Symptom.** Eval case `core_inflation` was rejected twice. Reading the
critique: it flagged "5 of the 6 readings at or above 3%" (true, derived from
the six listed values), listed one item that ended "No issue here", and in one
place did its own subtraction wrong.
**But also.** In the JOLTS case the same critic caught a real unit error
(7,437.97 thousand written as 7,437,970 thousand) and a false "lowest level"
claim. The critic is valuable *and* noisy.
**Fix.** Prompt now defines supported vs unsupported explicitly (derivable
arithmetic is fine), forbids non-issue entries, and ties `approved` to an
empty issue list. Tool output labels units on the summary line to prevent the
unit error at the source.
**Lesson.** An LLM judge is another model that can be wrong. Prefer
deterministic checks where possible (the regex citation check never
misfires), give the judge precise rubric boundaries, and measure its false
rejection rate with evals before trusting it as a gate.

## 13. `copy_thread` exists on the base class but not in SQLite
**Symptom.** `/thread rename` raised `NotImplementedError` from inside
`SqliteSaver.copy_thread` even though `hasattr(saver, "copy_thread")` was true.
**Cause.** The abstract base declares the method; the SQLite saver inherits the
raising default. Capability checks by attribute name lie for ABCs.
**Fix.** Rename via `UPDATE ... SET thread_id` on the `checkpoints` and
`writes` tables, guarded by a duplicate-name check; non-SQLite savers refuse.
**Lesson.** Test against the real backend, and prefer a try/except around the
operation over `hasattr` for optional capabilities.

## 14. LangGraph SDK: "Failed to construct 'URL': Invalid URL"
**Symptom.** First live run from the Next.js app threw inside `ThreadsClient.create`.
Demo mode, which never calls the SDK, was fine.
**Cause.** `useStream({ apiUrl: "/api" })`. The SDK builds `new URL(path, apiUrl)`,
and a relative base is not a valid URL. The passthrough route was correct; the
client never reached it.
**Fix.** `apiUrl: \`${window.location.origin}/api\`` (the component only mounts in
the browser).
**Lesson.** When a library "can't reach the server", check what URL it actually
constructed before debugging the server.

## 15. Next.js 16: `export const revalidate` vs Cache Components
**Symptom.** Build error "Route segment config 'revalidate' is not compatible with
nextConfig.cacheComponents" on the FRED dashboard route.
**Cause.** create-next-app 16 enables Cache Components, which replace the
per-segment `revalidate` export with `fetch(..., { next: { revalidate } })` and
`'use cache'`.
**Fix.** Dropped the export; the upstream fetch already carried its own revalidate.
**Lesson.** Framework defaults move; read the generated config before reaching for
a pattern from memory.

## 16. The Store transaction bug came back on a fresh database
**Symptom.** After renaming the project I moved the old checkpoint file aside.
The very next CLI run failed in `planner` with the same
"cannot start a transaction within a transaction" as note 11, now on the
Store's *own* connection.
**Cause.** Python's `sqlite3` default `isolation_level=""` opens an implicit
transaction before the first write. `SqliteStore.setup()` writes migration
rows on a brand-new file and leaves that implicit transaction open; the
store's next operation issues an explicit `BEGIN` and SQLite refuses. On the
old file the migrations already existed, so `setup()` wrote nothing and the
bug stayed hidden for a day.
**How found.** Reproduced in ten lines on a temp file with both isolation
settings; then read `SqliteStore.from_conn_string`, which passes
`isolation_level=None`.
**Fix.** Open the store's connection with `isolation_level=None`, as the
library does for itself.
**Lesson.** When a library offers a `from_conn_string` constructor, read it
before building the connection yourself: the arguments it chooses are the
contract. And a fix verified only against existing state is half verified;
test on an empty one too.

## 17. The planner emitted a step with no arguments
**Symptom.** On a follow-up question the plan was `compare_to_prepandemic({})`.
Fetch recorded a tool error as evidence, and the run went on to the analyst.
**Cause.** Structured output guarantees the *shape* of `Plan` (a list of steps
with a tool name and an args dict), not that the args satisfy the tool. Nothing
checked them before execution.
**Fix.** `_validate_steps` runs each step's args through the tool's own
`args_schema`; on failure the planner is called once more with the error text,
and steps that are still invalid are dropped rather than run.
**Lesson.** A schema for the container is not a schema for the contents.
Validate at the boundary where the real schema lives, which is the tool.

## 18. The analyst quoted the previous answer instead of evidence
**Symptom.** Same run as note 17: with only an error in the evidence block, the
draft stated three GDP figures with citations. They came from the earlier
answer in the thread, which I had just started passing to the analyst as
context. The critic rejected the draft twice and the answer shipped with the
reviewer note.
**Cause.** Conversation context and evidence were different things to me and
the same thing to the model. Anything in the prompt is fair game to cite.
**Fix.** Earlier turns' *evidence* (not their prose) is carried in state as
`prior_evidence`, shown to the analyst as evidence and included in the critic's
checks. The planner is told it exists, so follow-ups plan less and cite more.
**Lesson.** If you want the model to reuse something, give it the thing you
want cited, in the form you check. The critic doing its job is what made this
visible; without it the run would have looked fine.

## 19. The number whitelist leaked
**Symptom.** The new deterministic number check let "its 3.0% peak in late
2021" through with no 3.0 anywhere in the evidence.
**Cause.** Two leaks. Registry notes such as "3mo avg" and "12-month low" fed a
whitelist of "numbers the model may repeat", so 3 and 12 were always allowed.
And the scaled match divided 3.0 by 1,000 and landed within the absolute
tolerance of a *zero* difference produced by two identical evidence values.
**How found.** `evals/run_critic_evals.py` on labelled drafts, then a ten-line
trace of the one case. Precision stayed 1.0 throughout; recall went from 0.8
to 1.0 after the fix.
**Fix.** Whitelist only the question's numbers plus four named constants; drop
zero differences; apply scaling only above a magnitude.
**Lesson.** A check with a tolerance needs a labelled set on both sides. False
positives are visible immediately (they block answers); false negatives are
invisible until you measure them.

## 20. The model critic measured at precision 0.71
**Symptom.** With the deterministic number check at precision 1.0 and recall
1.0 on the labelled set, the model layer still rejected two correct drafts:
"five of the six readings are at or above 3%" (true, by counting) and "42%
below the 12,301 maximum in the window shown" (true, by arithmetic, and
qualified). Because either layer could reject, the combined gate inherited the
model's false positives, and the end-to-end evals showed it: four of 24
answers shipped with a reviewer note after two rejections.
**Cause.** A model judge applies a rubric unevenly; prompt examples lowered
the rate but did not fix it. Both layers were given equal authority over a
domain where one of them is exact.
**Fix.** Authority by competence. The deterministic layer verifies every
number it can; a model objection whose numbers are all verified is overruled
and logged. The model keeps authority over prose claims (trends, causes,
uncited history), which the regex cannot judge. Re-measured: model and
combined layers at precision 1.0, recall 1.0 on the labelled set.
**Lesson.** When you have an exact check and a judgement check for the same
facts, do not OR them. Let the exact one decide what it can decide, and scope
the judge to the rest. And measure the judge; the two false positives were
invisible until `run_critic_evals.py --llm` put a number on them.

## 21. "This model does not support assistant message prefill"
**Symptom.** A shared LangSmith trace of `macrodesk_v1` from Studio: status
error after 16 s, inputs `{"input": null}`, an Anthropic 400 saying the
conversation must end with a user message.
**Cause.** Studio re-ran the thread from its last checkpoint with no new input.
The thread's last message was the assistant's own answer, so the prebuilt
ReAct loop called the model with a conversation ending in an assistant turn.
Claude 4.6 and later treat that as a prefill and reject it. The Stage 2 graph is
immune because its planner builds a fresh system + human prompt every turn.
**How found.** `Client.list_shared_runs(share_token)` on the public link gave
the run, its metadata (`from_studio`, `graph_id`, `checkpoint_id`) and the
error text; no clicking required.
**Fix.** A `before_model` middleware on the v1 agent: if the last message is an
assistant message with no tool calls, jump to `end`. Resuming a finished
thread is now a no-op instead of a 400. Test added.
**Lesson.** `input: null` means "resume", and a resumed graph does whatever its
first node does with the existing state. Make that first node safe to re-enter.

## 22. Feedback remembered under the wrong user
**Symptom.** A scripted audit set the user id, asked a question, gave plan
feedback, and the sidebar never showed it as remembered.
**Cause.** The question's run carried `configurable.user_id`; the *resume*
run (approve or feedback) did not. The review node reads the user id from the
run it executes in, which is the resume, so the feedback was filed under
"default".
**Fix.** Every submit, including resumes and edited plans, passes the same
configurable.
**Lesson.** In LangGraph a resumed node runs under the config of the resume
request, not the original. Anything identity-shaped must travel with every
submit.

## 23. Stop closed the browser stream and the run kept going
**Symptom.** Clicking stop (or pressing Esc) returned the page to idle, but the
server finished the run and wrote the answer anyway; no cancel request was
ever sent.
**Cause.** With `reconnectOnMount` the SDK makes streams resumable and sets
disconnect behaviour to "continue", by design: a reload should be able to
reattach. Its own cancel path needs a stored run id that this flow never wrote.
**How found.** A Playwright probe that stops mid-analysis, waits 25 s, and
inspects the thread on the server: evidence present, draft and critic present
too. Then reading `manager.js` and `stream.lgp.js` for `stop`.
**Fix.** The app cancels explicitly: `client.runs.cancel(threadId, runId)` with
the run id from the metadata event, then `stream.stop()`. Re-probed: run status
`interrupted`, evidence fetched, no draft, no answer.
**Lesson.** "Stop" has two halves, the client stream and the server run, and a
library may only do one of them. Verify on the server, not in the UI.

## 24. My own rate limiter broke the app
**Symptom.** The live audit failed three unrelated checks, and the console
held one line: `HTTP 429 rate_limited`.
**Cause.** The passthrough bucket was 30 requests then one every four seconds.
One real session makes a burst per run: thread list, memory, series cards, the
stream, the cancel. The audit crossed the line in a minute; a person would in
an afternoon.
**Fix.** 120 requests then one per second, which still stops a loop.
**Lesson.** Size protective limits from a measured session, and run the
protection in the same audit as the features it protects.

## 25. Dependabot: a pin I set on day one became the biggest alert
**Symptom.** After a push GitHub reported 12 vulnerabilities, 3 critical.
Half were `cryptography` ranges in `uv.lock`, held back by the
`cryptography<46` constraint from note 2, because current releases ship no
wheel for Intel Macs. The rest were dev-only npm packages, most of them pulled
in by `mermaid`, which I had just added for the diagram check.
**Fix.** The Studio CLI is a developer tool, not something the agent imports,
so it left the project entirely: `scripts/studio.sh` layers it on with
`uv run --with` for one invocation (with the old pin only on Intel Macs), and `cryptography` is no longer in the
lock at all. On npm, upgrades plus `overrides` for the transitive packages;
`npm audit --omit=dev` is clean, and what remains is dev-only tooling behind
`eslint-config-next`.
**Lesson.** A workaround pin is a liability with a half-life. Separate tools
you run from libraries you ship, and let the alert list tell you which is
which.

## 26. A dependency that was never declared, revealed by a flag
**Symptom.** Right after the dependency cleanup, `next build` failed on Vercel
and in a clean `npm ci`: "Can't resolve '@langchain/core/messages'". Locally
everything had built for a week.
**Cause.** `@langchain/langgraph-sdk` imports `@langchain/core` but declares it
only as a peer dependency. npm 7+ installs peers automatically, so it had
always been there without being in `package.json`. Turning on
`legacy-peer-deps` (to get vitest 4 past a peer conflict) switched that off,
and the undeclared import surfaced.
**Fix.** Declare `@langchain/core` in `dependencies` at the SDK's peer range.
The clean-room `npm ci` now lists it; the build passes.
**Lesson.** Anything you import, you declare. Automatic peer installation is a
convenience that hides missing declarations until some unrelated setting
changes. A clean-room `npm ci` into an empty directory is the cheapest way to
find them.

## 27. The desk hung forever on "Connecting to the agent…"
**Symptom.** A new end-to-end test navigated from the monitor page back to the
desk and timed out: the page never left its loading state.
**Cause.** A LangGraph dev server from nine days earlier still held port 2024
but answered nothing. The passthrough's `fetch` had no deadline and neither did
the browser's mode probe, so a hung backend looked identical to a slow one,
forever. A *down* backend had always worked: connection refused fails fast.
**Fix.** Short passthrough requests get a deadline (4 s for `/info`, 20 s
otherwise; streaming runs stay open) and return 504 on timeout; the mode probe
aborts after 6 s and falls back to the replay. Then the stale server went.
**Lesson.** Test the failure you don't expect: "down" and "hung" are different,
and only the second needs a timeout to be survivable.

## Toolkit cheat-sheet

| Question | Tool |
|---|---|
| What did the model see / call? | terminal panels; `logs/run-<thread>.jsonl` |
| What is in state right now? | `/state` -> `graph.get_state(config)` |
| What happened N steps ago? | `/history` -> `graph.get_state_history(config)` |
| Re-run from an earlier point | `/replay N` -> stream with `checkpoint_id` in config |
| Approve/edit a plan mid-run | Stage 2 `review` interrupt -> `Command(resume=...)` |
| Visual step-through | `scripts/studio.sh` (which runs `langgraph dev` through `uv run --with`) -> Studio |
| Hosted traces, token costs | `LANGSMITH_TRACING=true` |
| Deterministic repro without the API | `tests/test_graph.py` scripted model pattern |
| Did a prompt change help or hurt? | `uv run python evals/run_evals.py` before and after |
| What does the agent remember about me? | `/prefs` in the REPL (Store namespace `prefs`) |
