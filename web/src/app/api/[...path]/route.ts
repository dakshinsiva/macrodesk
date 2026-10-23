/**
 * Passthrough to the LangGraph server.
 *
 * The browser talks to /api/*; this handler forwards to LANGGRAPH_API_URL and
 * attaches the LangSmith API key server-side, so the key never ships to the
 * client. Streaming responses (SSE from /runs/stream) are piped through.
 */
import { NextRequest } from "next/server";

const TARGET = process.env.LANGGRAPH_API_URL ?? "http://127.0.0.1:2024";

// Only what the research desk needs. Anything else (assistants CRUD, store writes, crons) is refused,
// so a public deployment exposes a narrow surface even though the server behind it is general.
const ALLOW: [RegExp, string[]][] = [
  [/^info$/, ["GET"]],
  [/^threads$/, ["POST"]],
  [/^threads\/search$/, ["POST"]],
  [/^threads\/[^/]+$/, ["GET"]],
  [/^threads\/[^/]+\/state$/, ["GET"]],
  [/^threads\/[^/]+\/history$/, ["GET", "POST"]],
  [/^threads\/[^/]+\/runs(\/stream|\/wait)?$/, ["POST"]],
  [/^threads\/[^/]+\/runs\/[^/]+(\/stream|\/join|\/cancel)?$/, ["GET", "POST"]],
  [/^store\/items\/search$/, ["POST"]],
  [/^assistants\/search$/, ["POST"]],
];

// Per-IP token bucket. A normal session makes a burst of small reads per run (thread list, memory,
// series cards, stream, cancel), so the bucket is sized for people, not for loops: 120 requests,
// then one per second. Lives per function instance; a global limiter would need a shared store.
const BUCKET = new Map<string, { tokens: number; at: number }>();
const RATE = { capacity: 120, refillPerSec: 1 };
function allow(ip: string): boolean {
  const now = Date.now();
  const b = BUCKET.get(ip) ?? { tokens: RATE.capacity, at: now };
  b.tokens = Math.min(RATE.capacity, b.tokens + ((now - b.at) / 1000) * RATE.refillPerSec);
  b.at = now;
  if (b.tokens < 1) { BUCKET.set(ip, b); return false; }
  b.tokens -= 1; BUCKET.set(ip, b); return true;
}

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const joined = path.join("/");
  const rule = ALLOW.find(([re]) => re.test(joined));
  if (!rule || !rule[1].includes(req.method)) {
    return Response.json({ error: "not_allowed", path: joined, method: req.method }, { status: 403 });
  }
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "local";
  if (!allow(ip)) return Response.json({ error: "rate_limited" }, { status: 429, headers: { "retry-after": "1" } });
  const url = new URL(`${TARGET.replace(/\/$/, "")}/${path.join("/")}`);
  url.search = req.nextUrl.search;

  const headers = new Headers();
  const ct = req.headers.get("content-type");
  if (ct) headers.set("content-type", ct);
  headers.set("accept", req.headers.get("accept") ?? "application/json");
  if (process.env.LANGSMITH_API_KEY) headers.set("x-api-key", process.env.LANGSMITH_API_KEY);

  // Short requests get a deadline so a hung backend fails fast (the desk then falls back to the demo);
  // streaming runs are left open and end when the client disconnects.
  const streaming = /\/runs\/(stream|[^/]+\/(stream|join))$/.test(joined) || /\/runs\/wait$/.test(joined);
  const signal = streaming ? req.signal : AbortSignal.any([req.signal, AbortSignal.timeout(joined === "info" ? 4000 : 20000)]);
  const init: RequestInit & { duplex?: "half" } = { method: req.method, headers, signal };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    init.duplex = "half";
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch (e) {
    const timedOut = (e as Error)?.name === "TimeoutError";
    return Response.json({ error: timedOut ? "backend_timeout" : "backend_unreachable", detail: String(e) }, { status: timedOut ? 504 : 502 });
  }
  const out = new Headers();
  for (const k of ["content-type", "cache-control", "content-encoding"]) {
    const v = upstream.headers.get(k);
    if (v) out.set(k, v);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
