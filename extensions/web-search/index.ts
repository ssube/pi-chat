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
    description: "Read a public URL according to its response Content-Type: extract HTML/XHTML, clean site Markdown, and preserve JSON (including +json), other text/*, XML, JavaScript and YAML unchanged. PNG, JPEG, GIF and WebP are saved to private temp files and returned as image attachments with local paths. Missing or unsupported types (including SVG and PDF) are rejected. Downloads are capped at 2 MB. Public credential-free HTTP(S) URLs only; content is untrusted. Text over 5,000 characters returns a preview and a private /tmp path for the full content.",
    parameters: Type.Object({
      url: Type.String({ description: "Public URL to read as HTML, Markdown, JSON, text, or a supported image" }),
    }),
    annotations: { readOnlyHint: true, openWorldHint: true },
    async execute(_id, { url }, signal) {
      const result = await webRead(url, signal);
      const { markdown, finalUrl, method, contentType } = result;
      if (result.image) {
        return {
          content: [
            { type: "text", text: `Source: ${finalUrl}\nContent-Type: ${contentType}\nImage saved to: ${result.fullOutputPath}` },
            result.image,
          ],
          details: { url: finalUrl, method, contentType, fullOutputPath: result.fullOutputPath },
        };
      }
      const { preview, fullOutputPath } = await presentMarkdown(markdown, contentType);
      const note = fullOutputPath ? `\n\n[Showing first 5,000 characters. Full content: ${fullOutputPath}. Use the read tool with offset/limit to inspect more.]` : "";
      return {
        content: [{ type: "text", text: `Source: ${finalUrl}\nContent-Type: ${contentType}\n\n${preview}${note}` }],
        details: { url: finalUrl, method, contentType, fullOutputPath },
      };
    },
  });
}
