"""Terminal REPL for the agent, with built-in debugging commands.

    uv run macrodesk "Is the labor market cooling? Use JOLTS."   # one-shot
    uv run macrodesk                                               # REPL
    uv run macrodesk --thread jolts-oct                            # resume (or start) a named thread
    uv run macrodesk --last                                        # resume the most recent thread
    uv run macrodesk --user alice                                  # per-user memory namespace
    uv run macrodesk --v1 "..."                                    # Stage 1 ReAct agent
    uv run macrodesk --auto-approve "..."                          # skip plan review
    uv run macrodesk brief                                         # write outputs/brief-<date>.md
    uv run macrodesk alerts [file.yaml]                            # evaluate alerts; exit 1 if any fire

Stage 2 graphs pause after planning: press Enter to approve the plan, or type
feedback (e.g. "use 10 years of history") to re-plan.

Debug commands inside the REPL:
    /state          show the current checkpoint's state summary
    /history        list checkpoints for this thread (newest first)
    /replay N       re-run from the Nth checkpoint in /history (time travel)
    /threads               list threads (newest first)
    /thread <name>         switch to (or create) a thread
    /thread new [name]     start a fresh thread
    /thread rename <name>  rename the current thread
    /thread delete <name>  delete a thread's checkpoints
    /prefs          show remembered plan feedback (long-term memory)
    /forget         clear remembered plan feedback
    /quit
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from macrodesk.config import get_settings
from macrodesk.brief import run_brief
from macrodesk.graph import build_graph, build_graph_v2
from macrodesk.memory import forget_all, recall_feedback
from macrodesk.persistence import open_persistence
from macrodesk.threads import delete_thread, latest_thread_id, list_threads, rename_thread
from macrodesk.logging import log_event, setup_logging

console = Console()
AUTO_APPROVE = False


USER_ID = "default"


def _cfg(thread_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    """Per-run configuration. thread_id selects the checkpoint lineage; user_id
    selects the long-term memory namespace. Both are plain `configurable` keys."""
    c: dict[str, Any] = {"configurable": {"thread_id": thread_id, "user_id": USER_ID}}
    if checkpoint_id:
        c["configurable"]["checkpoint_id"] = checkpoint_id
    return c


def _render_message(msg: BaseMessage) -> None:
    """Pretty-print one message emitted by a node update."""
    if isinstance(msg, AIMessage):
        for tc in msg.tool_calls:
            console.print(Panel(json.dumps(tc["args"], indent=1), title=f"[bold cyan]tool call -> {tc['name']}",
                                border_style="cyan"))
        text = msg.text
        if isinstance(text, str) and text.strip():
            console.print(Panel(Markdown(text), title="[bold green]assistant", border_style="green"))
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            log_event("model.usage", **{k: v for k, v in usage.items() if isinstance(v, int)})
    elif isinstance(msg, ToolMessage):
        content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        style = "red" if content.startswith("ERROR") else "yellow"
        console.print(Panel(content, title=f"[bold {style}]tool result <- {msg.name}", border_style=style))


def _render_update(node: str, update: Any) -> None:
    log_event("node.update", node=node)
    if node == "__interrupt__":
        return
    if not isinstance(update, dict):
        return
    for m in update.get("messages", []):
        _render_message(m)
    if "plan" in update and update["plan"] is not None:
        plan = update["plan"]
        steps = "\n".join(f"{i}. {s['tool']}({json.dumps(s['args'])})  - {s['why']}" for i, s in enumerate(plan["steps"], 1))
        console.print(Panel(f"[bold]{plan['goal']}[/]\n{steps}", title="[bold magenta]plan", border_style="magenta"))
    if "evidence" in update and update["evidence"]:
        for e in update["evidence"]:
            style = "yellow" if e["ok"] else "red"
            console.print(Panel(e["result"], title=f"[bold {style}]evidence <- {e['tool']}", border_style=style))
    if "draft" in update and update["draft"]:
        console.print(Panel(Markdown(update["draft"]), title=f"[bold blue]{node} draft", border_style="blue"))
    if "critique" in update and update["critique"] is not None:
        c = update["critique"]
        ok = c["approved"]
        body = "approved" if ok else "\n".join(f"- {i}" for i in c["issues"])
        console.print(Panel(body, title=f"[bold {'green' if ok else 'red'}]critic", border_style="green" if ok else "red"))


def _stream(graph, payload, cfg) -> bool:
    """Stream one graph run. Returns True if the run paused at an interrupt.
    'custom' events carry the analyst's tokens, printed live; 'updates' carry node results."""
    interrupted = False
    streaming = False
    for mode, chunk in graph.stream(payload, cfg, stream_mode=["updates", "custom"]):
        if mode == "custom":
            if chunk.get("draft_reset"):
                console.print("[dim]analyst is writing…[/]")
                streaming = True
            elif chunk.get("draft_delta"):
                console.print(chunk["draft_delta"], end="", style="dim", markup=False, highlight=False, soft_wrap=True)
            continue
        if streaming:
            console.print()  # end the token line before the panels
            streaming = False
        for node, update in chunk.items():
            if node == "__interrupt__":
                interrupted = True
            _render_update(node, update)
    return interrupted


def run_turn(graph, thread_id: str, user_input: str | None, checkpoint_id: str | None = None) -> None:
    """Stream one turn. user_input=None resumes/replays from a checkpoint.
    If the graph pauses for plan review, ask the human and resume until it finishes."""
    payload = {"messages": [HumanMessage(user_input)]} if user_input else None
    cfg = _cfg(thread_id, checkpoint_id)
    log_event("turn.start", thread_id=thread_id, input=user_input, checkpoint_id=checkpoint_id)
    interrupted = _stream(graph, payload, cfg)
    while interrupted:
        console.print("[magenta]Plan review:[/] press Enter to approve, or type feedback to re-plan.")
        if AUTO_APPROVE:
            decision = "approve"
        else:
            try:
                decision = console.input("[bold magenta]review>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                decision = "approve"
        interrupted = _stream(graph, Command(resume=decision or "approve"), _cfg(thread_id))
    log_event("turn.end", thread_id=thread_id)


def show_state(graph, thread_id: str) -> None:
    snap = graph.get_state(_cfg(thread_id))
    msgs = snap.values.get("messages", [])
    t = Table(title=f"state for thread {thread_id}", show_lines=False)
    t.add_column("#"); t.add_column("type"); t.add_column("preview")
    for i, m in enumerate(msgs):
        preview = str(m.text)[:90].replace("\n", " ")
        if isinstance(m, AIMessage) and m.tool_calls:
            preview = "tool_calls: " + ", ".join(tc["name"] for tc in m.tool_calls)
        t.add_row(str(i), type(m).__name__, preview)
    console.print(t)
    console.print(f"next node(s): {snap.next or '(idle)'}   checkpoint: {snap.config['configurable'].get('checkpoint_id')}")


def list_history(graph, thread_id: str) -> list[Any]:
    hist = list(graph.get_state_history(_cfg(thread_id)))
    t = Table(title=f"checkpoints for {thread_id} (0 = newest)")
    t.add_column("#"); t.add_column("step"); t.add_column("next"); t.add_column("msgs"); t.add_column("checkpoint_id")
    for i, s in enumerate(hist):
        t.add_row(str(i), str(s.metadata.get("step")), ",".join(s.next) or "-",
                  str(len(s.values.get("messages", []))), s.config["configurable"]["checkpoint_id"][-12:])
    console.print(t)
    return hist


def show_threads(graph, saver) -> None:
    t = Table(title="threads (newest first)")
    t.add_column("thread"); t.add_column("updated"); t.add_column("steps"); t.add_column("msgs"); t.add_column("first question")
    for th in list_threads(saver, graph):
        t.add_row(th.thread_id, th.updated.strftime("%Y-%m-%d %H:%M") if th.updated else "-",
                  str(th.steps), str(th.messages), th.first_question)
    console.print(t)


def repl(graph, thread_id: str, store=None, saver=None) -> None:
    console.print(Panel(f"macrodesk - thread [bold]{thread_id}[/]  (type /help)", border_style="blue"))
    hist: list[Any] = []
    while True:
        try:
            line = console.input("[bold blue]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/help":
            console.print(__doc__)
        elif line == "/state":
            show_state(graph, thread_id)
        elif line == "/history":
            hist = list_history(graph, thread_id)
        elif line.startswith("/replay"):
            parts = line.split()
            if len(parts) != 2 or not parts[1].isdigit():
                console.print("usage: /replay N   (run /history first)"); continue
            hist = hist or list(graph.get_state_history(_cfg(thread_id)))
            n = int(parts[1])
            if n >= len(hist):
                console.print(f"only {len(hist)} checkpoints"); continue
            snap = hist[n]
            console.print(f"[magenta]replaying from checkpoint {snap.config['configurable']['checkpoint_id'][-12:]} "
                          f"(next={snap.next})[/]")
            run_turn(graph, thread_id, None, snap.config["configurable"]["checkpoint_id"])
        elif line == "/prefs":
            prefs = recall_feedback(store)
            console.print(Panel("\n".join(f"- {p}" for p in prefs) or "(nothing remembered yet)",
                                title="[bold]remembered plan feedback", border_style="blue"))
        elif line == "/forget":
            console.print(f"forgot {forget_all(store)} item(s)")
        elif line == "/threads":
            show_threads(graph, saver)
        elif line.startswith("/thread"):
            parts = line.split(maxsplit=2)
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "new":
                thread_id = parts[2].strip() if len(parts) > 2 else uuid.uuid4().hex[:8]
            elif sub == "rename" and len(parts) == 3:
                try:
                    rename_thread(saver, thread_id, parts[2].strip())
                    thread_id = parts[2].strip()
                except NotImplementedError as e:
                    console.print(f"[red]{e}[/]"); continue
            elif sub == "delete" and len(parts) == 3:
                target = parts[2].strip()
                delete_thread(saver, target)
                console.print(f"deleted thread [bold]{target}[/]")
                if target == thread_id:
                    thread_id = uuid.uuid4().hex[:8]
                else:
                    continue
            elif sub and sub not in ("new", "rename", "delete"):
                thread_id = sub
            else:
                console.print("usage: /thread <name> | new [name] | rename <name> | delete <name>"); continue
            setup_logging(get_settings().log_dir, thread_id)
            console.print(f"thread [bold]{thread_id}[/]")
        else:
            run_turn(graph, thread_id, line)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="macrodesk", description="macrodesk: an agentic macro research desk over FRED")
    p.add_argument("question", nargs="*", help="one-shot question; omit for REPL")
    p.add_argument("--thread", default=None, help="thread name/id to resume or create (default: new random id)")
    p.add_argument("--last", action="store_true", help="resume the most recently updated thread")
    p.add_argument("--user", default="default", help="user id for long-term memory namespace")
    p.add_argument("--v1", action="store_true", help="use the Stage 1 prebuilt ReAct agent instead of the Stage 2 graph")
    p.add_argument("--auto-approve", action="store_true", help="skip the plan review prompt (Stage 2)")
    args = p.parse_args(argv)

    settings = get_settings()
    global USER_ID
    USER_ID = args.user

    persistence = open_persistence(settings)
    saver = persistence.saver
    thread_id = args.thread or (args.last and latest_thread_id(saver)) or uuid.uuid4().hex[:8]
    setup_logging(settings.log_dir, thread_id, settings.log_level)
    log_event("persistence", backend=persistence.backend)
    store = persistence.store
    graph = build_graph(checkpointer=saver) if args.v1 else build_graph_v2(checkpointer=saver, store=store)
    global AUTO_APPROVE
    AUTO_APPROVE = args.auto_approve

    if args.question and args.question[0] == "alerts":
        from macrodesk.alerts import evaluate, format_results, load_rules
        path = args.question[1] if len(args.question) > 1 else None
        results = evaluate(load_rules(path))
        print(format_results(results))  # plain stdout: stable columns for logs and cron mail
        sys.exit(1 if any(r.fired for r in results) else 0)
    if args.question and args.question[0] == "brief":
        AUTO_APPROVE = True
        path = run_brief(graph, thread_id, run_turn)
        console.print(f"[green]brief written to {path}[/]")
    elif args.question:
        run_turn(graph, thread_id, " ".join(args.question))
        console.print(f"[dim]thread {thread_id}  -  resume with: uv run macrodesk --thread {thread_id}[/]")
    else:
        repl(graph, thread_id, store, saver)


if __name__ == "__main__":
    main(sys.argv[1:])
