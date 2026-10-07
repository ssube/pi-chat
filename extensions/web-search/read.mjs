import { spawn } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { lookup as lookupAsync } from "node:dns/promises";
import { lookup } from "node:dns";
import { isIP } from "node:net";
import { fileURLToPath } from "node:url";
import { Agent } from "undici";

const CONVERTER = fileURLToPath(new URL("./html_to_markdown.py", import.meta.url));
const MAX_PAGE_BYTES = 2_000_000;
const PREVIEW_CHARS = 5_000;
const MAX_CONVERTER_OUTPUT = 4_000_000;

function publicIP(ip) {
  if (ip.startsWith("::ffff:")) return publicIP(ip.slice(7));
  if (isIP(ip) === 6) return !/^(::1$|::$|f[cd]|fe[89ab]|ff)/i.test(ip);
  const [a, b] = ip.split(".").map(Number);
  return isIP(ip) === 4 && a !== 0 && a !== 10 && a !== 127 && a < 224 && !(a === 169 && b === 254) && !(a === 172 && b >= 16 && b <= 31) && !(a === 192 && b === 168) && !(a === 100 && b >= 64 && b <= 127) && !(a === 198 && (b === 18 || b === 19));
}

export async function validatePublicUrl(input) {
  const url = new URL(input);
  const host = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  const blocked = (process.env.PI_WEB_READ_BLOCKED_SUFFIXES || "").toLowerCase().split(",").map((item) => item.trim()).filter(Boolean);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
      /(^|\.)(localhost|local|internal)$/.test(host) ||
      blocked.some((suffix) => host === suffix || host.endsWith(`.${suffix}`)) ||
      /(?:token|secret|password|api[_-]?key|signature|session)/i.test(url.search) ||
      url.href.length > 2048) throw new Error("web_read only accepts public, credential-free HTTP(S) URLs");
  const addresses = isIP(host) ? [{ address: host }] : await lookupAsync(host, { all: true });
  if (!addresses.length || addresses.some(({ address }) => !publicIP(address))) {
    throw new Error("web_read refuses private or non-public destinations");
  }
  return url;
}

// Enforce the same public-address rule on the DNS result used for the connection,
// not just on a separate preflight lookup (which could race DNS rebinding).
export function publicLookup(host, options, callback) {
  lookup(host, { ...options, all: true }, (error, addresses) => {
    if (error) return callback(error);
    if (!addresses?.length || addresses.some(({ address }) => !publicIP(address))) {
      return callback(new Error("web_read refuses private or non-public destinations"));
    }
    if (options.all) callback(null, addresses);
    else callback(null, addresses[0].address, addresses[0].family);
  });
}

async function bodyText(response) {
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > MAX_PAGE_BYTES) throw new Error("Page is too large to read safely");
      chunks.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  return new TextDecoder().decode(Buffer.concat(chunks));
}

export async function presentMarkdown(markdown) {
  if (markdown.length <= PREVIEW_CHARS) return { preview: markdown };
  const dir = await mkdtemp(join(tmpdir(), "pi-web-read-"));
  const fullOutputPath = join(dir, "page.md");
  await writeFile(fullOutputPath, markdown, { encoding: "utf8", mode: 0o600 });
  return { preview: markdown.slice(0, PREVIEW_CHARS), fullOutputPath };
}

export function cleanMarkdown(markdown) {
  return markdown
    .replace(/^\[Skip to content\]\([^\n]+\)\s*$/gm, "")
    .replace(/^.*\|Copy as Markdown\|.*$/gm, "")
    .replace(/```json\s*\n([\s\S]*?)```/g, (block, json) => /"@context"\s*:\s*"https?:\/\/schema\.org/.test(json) ? "" : block)
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

async function convertHtml(html, url, signal) {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [CONVERTER, url], { stdio: ["pipe", "pipe", "ignore"], signal });
    const timer = setTimeout(() => child.kill("SIGTERM"), 15000);
    let output = "";
    child.on("error", reject);
    child.stdout.on("data", (chunk) => {
      output += chunk.toString();
      if (output.length > MAX_CONVERTER_OUTPUT) child.kill("SIGTERM");
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) reject(new Error("HTML-to-Markdown conversion failed"));
      else resolve(output.trim());
    });
    child.stdin.end(html);
  });
}

export async function webRead(input, signal) {
  let url = await validatePublicUrl(input);
  const timeout = AbortSignal.timeout(25000);
  const combined = AbortSignal.any([signal ?? new AbortController().signal, timeout]);
  for (let redirects = 0; redirects <= 4; redirects++) {
    const dispatcher = new Agent({ connect: { lookup: publicLookup } });
    try {
      const response = await fetch(url, { headers: { Accept: "text/markdown, text/html;q=0.9" }, redirect: "manual", signal: combined, dispatcher });
      if (response.status >= 300 && response.status < 400) {
        if (redirects === 4 || !response.headers.get("location")) throw new Error("Too many redirects");
        await response.body?.cancel();
        url = await validatePublicUrl(new URL(response.headers.get("location"), url).href);
        continue;
      }
      if (!response.ok) throw new Error(`Web page returned HTTP ${response.status}`);
      const type = response.headers.get("content-type")?.toLowerCase() ?? "";
      if (!type.includes("text/markdown") && !type.includes("text/html")) throw new Error("Page did not return HTML or Markdown");
      const body = await bodyText(response);
      const method = type.includes("text/markdown") ? "site Markdown" : "HTML extraction";
      const markdown = type.includes("text/markdown") ? cleanMarkdown(body) : await convertHtml(body, url.href, combined);
      if (!markdown.trim()) throw new Error("No readable page content found");
      return { markdown, finalUrl: url.href, method };
    } finally {
      await dispatcher.close();
    }
  }
  throw new Error("Too many redirects");
}
