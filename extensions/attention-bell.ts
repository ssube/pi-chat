import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { attentionBell } from "./lib/attention-bell.js";

export default function (pi: ExtensionAPI) {
	// Not turn_end: that also fires between tool calls and automatic continuations.
	pi.on("agent_settled", (event, ctx) => {
		if (!("aborted" in event && event.aborted)) attentionBell(ctx);
	});
	pi.on("session_compact", (_event, ctx) => attentionBell(ctx));
}
