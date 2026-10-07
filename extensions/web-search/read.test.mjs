import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile, rm, stat } from "node:fs/promises";
import { dirname } from "node:path";
import { cleanMarkdown, presentMarkdown, publicLookup, validatePublicUrl } from "./read.mjs";

test("accepts public URL and rejects local, private, and credential-bearing destinations", async () => {
  assert.equal((await validatePublicUrl("https://1.1.1.1/path")).href, "https://1.1.1.1/path");
  for (const url of ["http://127.0.0.1", "http://10.0.0.1/", "http://[::1]/", "http://192.168.0.1/", "https://user:pass@example.com", "https://example.com/?api_key=secret", "file:///etc/passwd"]) {
    await assert.rejects(validatePublicUrl(url));
  }
});

test("owner-configured host suffixes are blocked without hardcoded domains", async () => {
  const previous = process.env.PI_WEB_READ_BLOCKED_SUFFIXES;
  try {
    process.env.PI_WEB_READ_BLOCKED_SUFFIXES = "1.1.1.1";
    await assert.rejects(validatePublicUrl("https://1.1.1.1/"), /public, credential-free/);
  } finally {
    if (previous === undefined) delete process.env.PI_WEB_READ_BLOCKED_SUFFIXES;
    else process.env.PI_WEB_READ_BLOCKED_SUFFIXES = previous;
  }
});

test("connection-time DNS lookup refuses private addresses", async () => {
  await assert.rejects(new Promise((resolve, reject) => {
    publicLookup("localhost", {}, (error, address) => error ? reject(error) : resolve(address));
  }), /private or non-public/);
});

test("removes site-provided navigation and schema metadata", () => {
  const source = '[Skip to content](#main-content)\n# Title\nLast updated|Copy as Markdown|[View as Markdown](https://example.com)\nUseful text\n```json\n{"@context":"https://schema.org","@type":"WebPage"}\n```';
  assert.equal(cleanMarkdown(source), '# Title\n\nUseful text');
});

test("short Markdown stays inline; long Markdown saves full content privately", async () => {
  assert.deepEqual(await presentMarkdown("small"), { preview: "small" });
  const markdown = "x".repeat(5001) + "\nlast line";
  const { preview, fullOutputPath } = await presentMarkdown(markdown);
  try {
    assert.equal(preview, markdown.slice(0, 5000));
    assert.equal(await readFile(fullOutputPath, "utf8"), markdown);
    assert.equal((await stat(dirname(fullOutputPath))).mode & 0o777, 0o700);
    assert.equal((await stat(fullOutputPath)).mode & 0o777, 0o600);
  } finally {
    await rm(dirname(fullOutputPath), { recursive: true, force: true });
  }
});
