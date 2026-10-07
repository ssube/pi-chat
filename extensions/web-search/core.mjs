import { spawn } from "node:child_process";

export function normalizeResults(rows, limit) {
  if (!Array.isArray(rows)) throw new Error("Search backend did not return a result array");
  return rows.slice(0, limit).map((row) => {
    const url = row?.url;
    if (typeof url !== "string" || !/^https?:\/\//.test(url) || url.length > 2048) {
      throw new Error("Search backend returned an invalid result URL");
    }
    return {
      title: String(row.title ?? "").slice(0, 250),
      url,
      snippet: String(row.snippet ?? row.content ?? "").slice(0, 500),
    };
  });
}

export function parseCodexEvents(output, limit) {
  let searched = false;
  let final;
  for (const line of output.split("\n")) {
    if (!line.trim()) continue;
    const event = JSON.parse(line);
    if (event.type === "item.completed" && event.item?.type === "web_search") searched = true;
    if (event.type === "item.completed" && event.item?.type === "agent_message") final = event.item.text;
    if (event.type === "turn.failed") throw new Error("Codex web search failed");
  }
  if (!searched || !final) throw new Error("Codex did not complete a native web search");
  return normalizeResults(JSON.parse(final), limit);
}

export async function searchCodex(query, limit, signal) {
  const prompt = `Use the native web_search tool to search for the query below. Do not run shell commands or read local files. Return ONLY a JSON array of up to ${limit} results in search order with title, url, and snippet strings. No Markdown or prose.\nQuery: ${JSON.stringify(query)}`;
  const args = ["--search", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "-C", "/tmp", "--sandbox", "read-only", "--json", "-"];
  const output = await new Promise((resolve, reject) => {
    const child = spawn("codex", args, { stdio: ["pipe", "pipe", "ignore"], signal });
    let stdout = "";
    const timer = setTimeout(() => child.kill("SIGTERM"), 90000);
    child.on("error", reject);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
      if (stdout.length > 2_000_000) child.kill("SIGTERM");
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) reject(new Error("Codex web search exited unsuccessfully"));
      else resolve(stdout);
    });
    child.stdin.end(prompt);
  });
  return parseCodexEvents(output, limit);
}

export async function searchSearxng(query, limit, signal) {
  const base = process.env.SEARXNG_URL;
  if (!base) throw new Error("SEARXNG_URL is required when PI_WEB_SEARCH_BACKEND=searxng");
  const url = new URL("search", base.endsWith("/") ? base : base + "/");
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error("SEARXNG_URL must use HTTP or HTTPS");
  url.searchParams.set("q", query);
  url.searchParams.set("format", "json");
  const response = await fetch(url, { headers: { Accept: "application/json" }, signal: AbortSignal.any([signal ?? new AbortController().signal, AbortSignal.timeout(20000)]) });
  if (!response.ok) throw new Error(`SearXNG returned HTTP ${response.status}`);
  const data = await response.json();
  return normalizeResults(data.results, limit);
}

export async function webSearch(query, limit, signal) {
  if (typeof query !== "string" || !query.trim() || query.length > 300 || /[\r\n\x00-\x1f]/.test(query)) {
    throw new Error("Search query must be 1–300 characters on one line");
  }
  if (!Number.isInteger(limit) || limit < 1 || limit > 10) throw new Error("Result count must be 1–10");
  const backend = process.env.PI_WEB_SEARCH_BACKEND || "codex";
  if (backend === "codex") return searchCodex(query, limit, signal);
  if (backend === "searxng") return searchSearxng(query, limit, signal);
  throw new Error("PI_WEB_SEARCH_BACKEND must be codex or searxng");
}
