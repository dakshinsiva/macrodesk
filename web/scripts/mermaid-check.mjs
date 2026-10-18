// Validate every ```mermaid block in markdown files with Mermaid's own parser (the engine GitHub uses).
// Usage: node web/scripts/mermaid-check.mjs README.md docs/*.md
import { chromium } from "playwright-core";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
const here = path.dirname(fileURLToPath(import.meta.url));
const files = process.argv.slice(2);
const browser = await chromium.launch(); const page = await browser.newPage();
await page.setContent("<html><body></body></html>");
const dist = process.env.MERMAID_PATH ?? path.join(here, "..", "node_modules", "mermaid", "dist", "mermaid.min.js");
await page.addScriptTag({ path: dist });  // no network needed
await page.waitForFunction(() => typeof window.mermaid !== "undefined");
let bad = 0;
for (const f of files) {
  const blocks = [...readFileSync(f, "utf8").matchAll(/```mermaid\n([\s\S]*?)```/g)].map((m) => m[1]);
  for (const [i, b] of blocks.entries()) {
    // parse, then actually render: some inputs parse and still fail at render time
    const r = await page.evaluate(async ([src, i]) => {
      try { await window.mermaid.parse(src); } catch (e) { return "parse: " + String(e.message || e); }
      try { const { svg } = await window.mermaid.render("m" + i, src); if (!svg.includes("<svg")) return "render: empty svg"; return "ok"; } catch (e) { return "render: " + String(e.message || e); }
    }, [b, i]);
    console.log(`${f} block ${i + 1}: ${r === "ok" ? "OK" : "ERROR " + r.split("\n")[0].slice(0, 200)}`);
    if (r !== "ok") bad++;
  }
}
await browser.close();
process.exit(bad ? 1 : 0);
