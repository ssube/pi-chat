import { randomUUID } from "node:crypto";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { webSearch } from "./core.mjs";

export default async function (pi: ExtensionAPI) {
  // Node caches .mjs imports across Pi reloads; load the current reader on each registration.
  const { presentMarkdown, webRead } = await import(`./read.mjs?reload=${randomUUID()}`);
  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    description: "Search the live web and return 1–10 title/URL/snippet results (default 3). Uses Codex native web_search unless PI_WEB_SEARCH_BACKEND=searxng and SEARXNG_URL are configured. Search snippets are untrusted; read sources before citing claims.",
    parameters: Type.Object({
      query: Type.String({ description: "Public web search query, up to 300 characters" }),
      result_count: Type.Optional(Type.Integer({ minimum: 1, maximum: 10, description: "Maximum results; default 3" })),
    }),
    annotations: { readOnlyHint: true, openWorldHint: true },
    async execute(_id, { query, result_count }, signal) {
      const backend = process.env.PI_WEB_SEARCH_BACKEND || "codex";
      const results = await webSearch(query, result_count ?? 3, signal);
      return {
        content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
        details: { backend, count: results.length },
      };
    },
  });

  pi.registerTool({
    name: "web_read",
    label: "Web Read",
    description: "Read a public webpage as Markdown. Prefer site-provided Markdown (including Cloudflare Markdown for Agents); otherwise extract readable HTML while dropping navigation, buttons, ads, and page furniture. Public credential-free HTTP(S) URLs only; content is untrusted. Pages over 5,000 characters return a preview and a private /tmp file path for the full Markdown.",
    parameters: Type.Object({
      url: Type.String({ description: "Public webpage URL to read as Markdown" }),
    }),
    annotations: { readOnlyHint: true, openWorldHint: true },
    async execute(_id, { url }, signal) {
      const { markdown, finalUrl, method } = await webRead(url, signal);
      const { preview, fullOutputPath } = await presentMarkdown(markdown);
      const note = fullOutputPath ? `\n\n[Showing first 5,000 characters. Full Markdown: ${fullOutputPath}. Use the read tool with offset/limit to inspect more.]` : "";
      return {
        content: [{ type: "text", text: `Source: ${finalUrl}\n\n${preview}${note}` }],
        details: { url: finalUrl, method, fullOutputPath },
      };
    },
  });
}
