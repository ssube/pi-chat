import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";
import { normalizeResults, parseCodexEvents, webSearch } from "./core.mjs";

const rows = Array.from({ length: 12 }, (_, i) => ({ title: `Result ${i}`, url: `https://example.com/${i}`, content: `Excerpt ${i}` }));

test("normalizes SearXNG content and caps result count", () => {
  assert.deepEqual(normalizeResults(rows, 1), [{ title: "Result 0", url: "https://example.com/0", snippet: "Excerpt 0" }]);
  assert.equal(normalizeResults(rows, 10).length, 10);
  assert.throws(() => normalizeResults([{ title: "bad", url: "file:///etc/passwd" }], 1), /invalid result URL/);
});

test("requires an actual Codex web_search event and valid JSON", () => {
  const final = JSON.stringify([{ title: "Result", url: "https://example.com", snippet: "Text" }]);
  const message = JSON.stringify({ type: "item.completed", item: { type: "agent_message", text: final } });
  const search = JSON.stringify({ type: "item.completed", item: { type: "web_search" } });
  assert.throws(() => parseCodexEvents(message, 3), /native web search/);
  assert.deepEqual(parseCodexEvents(`${search}\n${message}`, 3), JSON.parse(final));
});

test("SearXNG returns the same schema with configurable count", async () => {
  const server = createServer((request, response) => {
    const url = new URL(request.url, "http://localhost");
    assert.equal(url.pathname, "/search");
    assert.equal(url.searchParams.get("q"), "test query");
    assert.equal(url.searchParams.get("format"), "json");
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ results: rows }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const before = [process.env.PI_WEB_SEARCH_BACKEND, process.env.SEARXNG_URL];
  try {
    process.env.PI_WEB_SEARCH_BACKEND = "searxng";
    process.env.SEARXNG_URL = `http://127.0.0.1:${server.address().port}`;
    assert.equal((await webSearch("test query", 3)).length, 3);
    assert.equal((await webSearch("test query", 10)).length, 10);
    await assert.rejects(webSearch("test query", 11), /1–10/);
  } finally {
    for (const [key, value] of [["PI_WEB_SEARCH_BACKEND", before[0]], ["SEARXNG_URL", before[1]]]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await new Promise((resolve) => server.close(resolve));
  }
});
