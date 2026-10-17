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
// HTML extraction needs BeautifulSoup, declared in the converter's own PEP 723
// metadata. Run it through uv so the dependency resolves from the machine's uv
// cache instead of from the session PATH; the argument list stays explicit, so
// the session's own python3 remains first for the agent and everything else it
// runs. PI_WEB_READ_PYTHON overrides the runner and may hold a whole command.
export function converterCommand() {
  return (process.env.PI_WEB_READ_PYTHON || "uv run --script").split(/\s+/).filter(Boolean);
}
const MAX_PAGE_BYTES = 2_000_000;
const IMAGE_EXTENSIONS = new Map([
  ["image/png", "png"], ["image/jpeg", "jpg"], ["image/gif", "gif"], ["image/webp", "webp"],
]);
const PREVIEW_CHARS = 5_000;
const MAX_CONVERTER_OUTPUT = 4_000_000;
const MAX_CONVERTER_ERROR = 500;

// Fold the runner's leading stderr into the conversion error so a real cause
// (a cold uv cache, a missing interpreter) is not reduced to a generic message.
function converterFailure(stderr) {
  const text = stderr.trim().replace(/\s+/g, " ");
  return text ? ": " + text.slice(0, MAX_CONVERTER_ERROR) : "";
}

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

async function bodyBytes(response) {
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
  return Buffer.concat(chunks);
}

// Verify the declared format's signature, not the URL suffix. This is a
// lightweight mismatch check, not a full image decoder or content sniffer.
function imageSignatureMatches(bytes, type) {
  if (type === "image/png") return bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"));
  if (type === "image/jpeg") return bytes.subarray(0, 3).equals(Buffer.from("ffd8ff", "hex"));
  if (type === "image/gif") return ["GIF87a", "GIF89a"].includes(bytes.subarray(0, 6).toString("latin1"));
  return bytes.subarray(0, 4).toString("latin1") === "RIFF" && bytes.subarray(8, 12).toString("latin1") === "WEBP";
}

function contentKind(type) {
  if (IMAGE_EXTENSIONS.has(type)) return "image";
  if (type === "text/html" || type === "application/xhtml+xml") return "HTML extraction";
  if (type === "text/markdown") return "site Markdown";
  if (/^application\/(?:json|[\w.+-]+\+json)$/.test(type)) return "JSON";
  if (type.startsWith("text/") || /^application\/(?:xml|[\w.+-]+\+xml|javascript|yaml|x-yaml)$/.test(type)) return "text";
  return undefined;
}

export async function presentMarkdown(markdown, contentType = "text/markdown") {
  if (markdown.length <= PREVIEW_CHARS) return { preview: markdown };
  const dir = await mkdtemp(join(tmpdir(), "pi-web-read-"));
  const kind = contentKind(contentType);
  const extension = kind === "JSON" ? "json" : kind === "text" ? "txt" : "md";
  const fullOutputPath = join(dir, `page.${extension}`);
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

export async function convertHtml(html, url, signal) {
  return new Promise((resolve, reject) => {
    const [runner, ...runnerArgs] = converterCommand();
    const child = spawn(runner, [...runnerArgs, CONVERTER, url], { stdio: ["pipe", "pipe", "pipe"], signal });
    const timer = setTimeout(() => child.kill("SIGTERM"), 15000);
    let output = "";
    let stderr = "";
    child.on("error", reject);
    child.stdout.on("data", (chunk) => {
      output += chunk.toString();
      if (output.length > MAX_CONVERTER_OUTPUT) child.kill("SIGTERM");
    });
    child.stderr.on("data", (chunk) => {
      // Keep the head; a runner reports the cause at the start of stderr.
      if (stderr.length < MAX_CONVERTER_ERROR * 4) {
        stderr = (stderr + chunk.toString()).slice(0, MAX_CONVERTER_ERROR * 4);
      }
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) reject(new Error("HTML-to-Markdown conversion failed" + converterFailure(stderr)));
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
      const response = await fetch(url, { headers: { Accept: "text/markdown, text/html;q=0.9, application/xhtml+xml;q=0.9, application/json;q=0.8, text/*;q=0.7, application/xml;q=0.7, image/png;q=0.6, image/jpeg;q=0.6, image/gif;q=0.6, image/webp;q=0.6" }, redirect: "manual", signal: combined, dispatcher });
      if (response.status >= 300 && response.status < 400) {
        if (redirects === 4 || !response.headers.get("location")) throw new Error("Too many redirects");
        await response.body?.cancel();
        url = await validatePublicUrl(new URL(response.headers.get("location"), url).href);
        continue;
      }
      if (!response.ok) throw new Error(`Web page returned HTTP ${response.status}`);
      const contentType = (response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
      const method = contentKind(contentType);
      if (!method) {
        await response.body?.cancel();
        throw new Error(`Unsupported content type: ${contentType.slice(0, 100) || "missing"}`);
      }
      const bytes = await bodyBytes(response);
      if (method === "image") {
        if (!imageSignatureMatches(bytes, contentType)) throw new Error("Image signature does not match Content-Type");
        const dir = await mkdtemp(join(tmpdir(), "pi-web-read-"));
        const fullOutputPath = join(dir, `image.${IMAGE_EXTENSIONS.get(contentType)}`);
        await writeFile(fullOutputPath, bytes, { mode: 0o600 });
        return {
          image: { type: "image", data: bytes.toString("base64"), mimeType: contentType },
          fullOutputPath, finalUrl: url.href, method, contentType,
        };
      }
      const body = new TextDecoder().decode(bytes);
      // Keep non-HTML data verbatim: parsing JSON can lose integer precision,
      // and Markdown cleanup can remove meaningful data or whitespace.
      const markdown = method === "HTML extraction" ? await convertHtml(body, url.href, combined)
        : method === "site Markdown" ? cleanMarkdown(body) : body;
      if (!markdown.trim()) throw new Error("No readable page content found");
      return { markdown, finalUrl: url.href, method, contentType };
    } finally {
      await dispatcher.close();
    }
  }
  throw new Error("Too many redirects");
}
