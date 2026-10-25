import type { ExtensionContext } from "@earendil-works/pi-coding-agent";

// Never put control bytes into RPC/JSON output or redirected transcripts.
export function attentionBell(ctx: Pick<ExtensionContext, "mode">, output = process.stdout): void {
	if (ctx.mode !== "tui" || !output.isTTY) return;
	try { output.write("\x07"); } catch { /* An unavailable terminal must not fail the event. */ }
}
