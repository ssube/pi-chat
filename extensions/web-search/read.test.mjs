import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { cleanMarkdown, converterCommand, convertHtml, presentMarkdown, publicLookup, validatePublicUrl, webRead } from "./read.mjs";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOQS7EBAAFiAL/aKhDnAAAAAElFTkSuQmCC", "base64");
const IMAGES = [
  ["image/png", "png", PNG],
  ["image/jpeg", "jpg", Buffer.from("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDiqKKKwPDP/9k=", "base64")],
  ["image/gif", "gif", Buffer.from("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7", "base64")],
  ["image/webp", "webp", Buffer.from("UklGRjQAAABXRUJQVlA4ICgAAABwAQCdASoBAAEAAUAmJaACdAFAAAD+899iS6XIb/8Wh/bz/g1tKAAA", "base64")],
];

test("webRead saves supported raster images privately and returns image content without text decoding", async (t) => {
  for (const [mimeType, extension, bytes] of IMAGES) {
    t.mock.method(globalThis, "fetch", async (_url, options) => {
      assert.match(options.headers.Accept, new RegExp(mimeType));
      return new Response(bytes, { headers: { "content-type": `${mimeType.toUpperCase()}; ignored=parameter` } });
    });
    const result = await webRead("https://1.1.1.1/unsafe-name.php");
    try {
      assert.equal(result.method, "image");
      assert.equal(result.contentType, mimeType);
      assert.equal(result.markdown, undefined);
      assert.deepEqual(result.image, { type: "image", data: bytes.toString("base64"), mimeType });
      assert.equal(result.fullOutputPath.endsWith(`/image.${extension}`), true);
      assert.deepEqual(await readFile(result.fullOutputPath), bytes);
      assert.equal((await stat(dirname(result.fullOutputPath))).mode & 0o777, 0o700);
      assert.equal((await stat(result.fullOutputPath)).mode & 0o777, 0o600);
    } finally {
      if (result.fullOutputPath) await rm(dirname(result.fullOutputPath), { recursive: true, force: true });
      t.mock.restoreAll();
    }
  }
});

test("webRead rejects empty, mismatched, and oversized image bodies", async (t) => {
  for (const [mimeType, bytes, error] of [
    ["image/png", Buffer.alloc(0), /Image signature/],
    ["image/png", Buffer.from("<html>not an image</html>"), /Image signature/],
    ["image/jpeg", PNG, /Image signature/],
    ["image/webp", Buffer.from("RIFFxxxxxxxxNOTWEBP"), /Image signature/],
    ["image/gif", Buffer.from("c7c9c6b8b9e1", "hex"), /Image signature/],
    ["image/png", Buffer.alloc(2_000_001), /too large/],
  ]) {
    t.mock.method(globalThis, "fetch", async () => new Response(bytes, { headers: { "content-type": mimeType } }));
    await assert.rejects(webRead("https://1.1.1.1/image"), error);
    t.mock.restoreAll();
  }
});

test("webRead applies public-URL checks to redirected images", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => ++calls === 1
    ? new Response(null, { status: 302, headers: { location: "/picture.png" } })
    : new Response(PNG, { headers: { "content-type": "image/png" } }));
  const result = await webRead("https://1.1.1.1/start");
  try {
    assert.equal(result.finalUrl, "https://1.1.1.1/picture.png");
    assert.equal(result.image.mimeType, "image/png");
  } finally {
    if (result.fullOutputPath) await rm(dirname(result.fullOutputPath), { recursive: true, force: true });
    t.mock.restoreAll();
  }
  t.mock.method(globalThis, "fetch", async () => new Response(null, {
    status: 302, headers: { location: "http://127.0.0.1/private.png" },
  }));
  await assert.rejects(webRead("https://1.1.1.1/start"), /private or non-public/);
});

test("webRead preserves JSON and other textual responses without HTML or Markdown cleanup", async (t) => {
  const body = '  {"html":"<script>keep me</script>","id":9007199254740993}\n\n\n';
  for (const contentType of ["application/json", "Application/Problem+JSON; charset=utf-8", "text/plain", "text/csv", "application/xml", "application/atom+xml", "application/javascript", "application/yaml"]) {
    t.mock.method(globalThis, "fetch", async (_url, options) => {
      assert.match(options.headers.Accept, /application\/json/);
      return new Response(body, { headers: { "content-type": contentType } });
    });
    try {
      const result = await webRead("https://1.1.1.1/data");
      assert.equal(result.markdown, body);
      assert.equal(result.contentType, contentType.split(";")[0].trim().toLowerCase());
      assert.notEqual(result.method, "HTML extraction");
    } finally {
      t.mock.restoreAll();
    }
  }
});

test("webRead extracts only HTML and XHTML through the converter", async (t) => {
  const previous = process.env.PI_WEB_READ_PYTHON;
  try {
    process.env.PI_WEB_READ_PYTHON = previous || "python3";
    for (const contentType of ["text/html; charset=utf-8", "application/xhtml+xml"]) {
      t.mock.method(globalThis, "fetch", async () => new Response('<html><body><nav>Discard</nav><main><h1>Title</h1><p>Keep this.</p></main></body></html>', {
        headers: { "content-type": contentType },
      }));
      const result = await webRead("https://1.1.1.1/page");
      assert.equal(result.method, "HTML extraction");
      assert.match(result.markdown, /# Title/);
      assert.match(result.markdown, /Keep this\./);
      assert.doesNotMatch(result.markdown, /Discard|<html>/);
      t.mock.restoreAll();
    }
  } finally {
    if (previous === undefined) delete process.env.PI_WEB_READ_PYTHON;
    else process.env.PI_WEB_READ_PYTHON = previous;
  }
});

test("webRead handles JSON after a validated redirect and still rejects private redirects", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (url) => {
    calls++;
    if (calls === 1) return new Response(null, { status: 302, headers: { location: "/data.json" } });
    assert.equal(url.href, "https://1.1.1.1/data.json");
    return new Response('{"ok":true}', { headers: { "content-type": "application/json" } });
  });
  const result = await webRead("https://1.1.1.1/start");
  assert.equal(result.finalUrl, "https://1.1.1.1/data.json");
  assert.equal(result.markdown, '{"ok":true}');
  t.mock.restoreAll();
  t.mock.method(globalThis, "fetch", async () => new Response(null, {
    status: 302, headers: { location: "http://127.0.0.1/private.json" },
  }));
  await assert.rejects(webRead("https://1.1.1.1/start"), /private or non-public/);
});

test("webRead rejects binary, missing and misleading MIME types without reading their bodies", async (t) => {
  for (const contentType of ["application/pdf", "image/svg+xml", "image/avif", "application/octet-stream", "application/jsonp", "application/x-text/html", ""]) {
    let cancelled = false;
    t.mock.method(globalThis, "fetch", async () => ({
      status: 200, ok: true,
      headers: new Headers(contentType ? { "content-type": contentType } : {}),
      body: { cancel: async () => { cancelled = true; }, getReader: () => assert.fail("unsupported body read") },
    }));
    try {
      await assert.rejects(webRead("https://1.1.1.1/data"), /Unsupported content type/);
      assert.equal(cancelled, true);
    } finally {
      t.mock.restoreAll();
    }
  }
});

test("webRead still cleans site Markdown and enforces the body limit for JSON", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response('[Skip to content](#main)\n# Title', {
    headers: { "content-type": "Text/Markdown; charset=UTF-8" },
  }));
  const result = await webRead("https://1.1.1.1/page");
  assert.equal(result.markdown, "# Title");
  assert.equal(result.method, "site Markdown");
  t.mock.restoreAll();
  t.mock.method(globalThis, "fetch", async () => new Response("x".repeat(2_000_001), {
    headers: { "content-type": "application/json" },
  }));
  await assert.rejects(webRead("https://1.1.1.1/data"), /too large/);
});

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

test("HTML converter runs uv by default and takes an explicit command override", () => {
  const previous = process.env.PI_WEB_READ_PYTHON;
  try {
    delete process.env.PI_WEB_READ_PYTHON;
    assert.deepEqual(converterCommand(), ["uv", "run", "--script"]);
    process.env.PI_WEB_READ_PYTHON = "/opt/web/bin/python3";
    assert.deepEqual(converterCommand(), ["/opt/web/bin/python3"]);
    process.env.PI_WEB_READ_PYTHON = "uv run --script --python /opt/web/bin/python3";
    assert.deepEqual(converterCommand(), ["uv", "run", "--script", "--python", "/opt/web/bin/python3"]);
  } finally {
    if (previous === undefined) delete process.env.PI_WEB_READ_PYTHON;
    else process.env.PI_WEB_READ_PYTHON = previous;
  }
});

test("converter failures carry the runner's own stderr", async () => {
  const previous = process.env.PI_WEB_READ_PYTHON;
  const dir = await mkdtemp(join(tmpdir(), "pi-web-read-fail-"));
  const failing = join(dir, "failing.mjs");
  await writeFile(failing,
    'process.stderr.write("error: No solution found when resolving script dependencies\\n");\nprocess.exit(3);\n',
    { mode: 0o600 });
  try {
    process.env.PI_WEB_READ_PYTHON = `${process.execPath} ${failing}`;
    await assert.rejects(convertHtml("<html></html>", "https://example.com/"),
      /HTML-to-Markdown conversion failed: error: No solution found when resolving script dependencies/);
  } finally {
    if (previous === undefined) delete process.env.PI_WEB_READ_PYTHON;
    else process.env.PI_WEB_READ_PYTHON = previous;
    await rm(dir, { recursive: true, force: true });
  }
});

test("removes site-provided navigation and schema metadata", () => {
  const source = '[Skip to content](#main-content)\n# Title\nLast updated|Copy as Markdown|[View as Markdown](https://example.com)\nUseful text\n```json\n{"@context":"https://schema.org","@type":"WebPage"}\n```';
  assert.equal(cleanMarkdown(source), '# Title\n\nUseful text');
});

test("long JSON saves an exact, private JSON file", async () => {
  const body = '{"text":"' + "x".repeat(6000) + '"}\n';
  const { preview, fullOutputPath } = await presentMarkdown(body, "application/problem+json");
  try {
    assert.equal(preview, body.slice(0, 5000));
    assert.equal(fullOutputPath.endsWith("/page.json"), true);
    assert.equal(await readFile(fullOutputPath, "utf8"), body);
    assert.equal((await stat(fullOutputPath)).mode & 0o777, 0o600);
  } finally {
    await rm(dirname(fullOutputPath), { recursive: true, force: true });
  }
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
