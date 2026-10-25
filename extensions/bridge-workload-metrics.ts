import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// Counts only, over Pi's existing fire-and-forget RPC status channel. No model
// calls, tools, transcript reads, files or TUI status changes.
const KEY = "agent-bridge/workload/counts/v1";
const PERIOD_MS = 15_000;
const REPLY_MS = 1_000;
const STATES = ["pending", "progress", "blocked", "question", "complete"] as const;
const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

export default function (pi: ExtensionAPI) {
	let timer: ReturnType<typeof setInterval> | undefined;
	let cancel: (() => void) | undefined;
	let generation = 0;
	let busy = false;
	let context: ExtensionContext | undefined;
	let changed: ReturnType<typeof setTimeout> | undefined;

	function stop() {
		generation++;
		if (timer) clearInterval(timer);
		timer = undefined;
		if (changed) clearTimeout(changed);
		changed = undefined;
		cancel?.(); cancel = undefined;
		if (context?.mode === "rpc") context.ui.setStatus(KEY, undefined);
		context = undefined;
		busy = false;
	}

	async function update(ctx: ExtensionContext) {
		if (ctx.mode !== "rpc" || context !== ctx || busy) return;
		busy = true;
		const current = generation;
		try {
			const query: { version: number; counts?: Record<string, number> } = { version: 1 };
			pi.events.emit("tasks:metrics-v1", query);
			const tasks = query.counts && Object.keys(query.counts).length === STATES.length &&
				STATES.every(state => count(query.counts?.[state]))
				? Object.fromEntries(STATES.map(state => [state, query.counts![state]])) : null;
			const requestId = randomUUID();
			const subagents = await new Promise<{ active: number; capacity_used: number; capacity_limit: number } | null>(resolve => {
				let done = false;
				let timeout: ReturnType<typeof setTimeout>;
				let unsubscribe: (() => void) | undefined;
				const finish = (value: { active: number; capacity_used: number; capacity_limit: number } | null) => {
					if (done) return;
					done = true; clearTimeout(timeout); unsubscribe?.(); cancel = undefined; resolve(value);
				};
				cancel = () => finish(null);
				timeout = setTimeout(() => finish(null), REPLY_MS);
				unsubscribe = pi.events.on(`subagents:rpc:v1:reply:${requestId}`, (raw: any) => {
					const fleet = raw?.data?.fleet;
					const capacity = fleet?.topLevelAsyncCapacity;
					finish(raw?.version === 1 && raw?.requestId === requestId && raw?.success === true && fleet?.version === 1 &&
						count(fleet.totalActive) && count(capacity?.used) && count(capacity?.limit)
						? { active: fleet.totalActive, capacity_used: capacity.used, capacity_limit: capacity.limit } : null);
				});
				pi.events.emit("subagents:rpc:v1:request", { version: 1, requestId, method: "status", params: {}, source: { extension: "bridge-workload-metrics" } });
			});
			if (current === generation) ctx.ui.setStatus(KEY, JSON.stringify({ version: 1, tasks, subagents }));
		} catch {
			// An unavailable observer must not interrupt the session or report zero.
			if (current === generation) ctx.ui.setStatus(KEY, JSON.stringify({ version: 1, tasks: null, subagents: null }));
		} finally {
			if (current === generation) busy = false;
		}
	}

	function start(ctx: ExtensionContext) {
		stop();
		if (ctx.mode !== "rpc") return;
		context = ctx;
		// Let all session_start handlers restore their authoritative state first.
		timer = setInterval(() => void update(ctx), PERIOD_MS);
		timer.unref();
		queueMicrotask(() => void update(ctx));
	}
	// Coalesce bursts; never emit one RPC frame for every child/task transition.
	for (const event of ["tasks:changed-v1", "questions:changed-v1", "subagent:child-status", "subagent:async-complete", "subagent:process-terminal"]) {
		pi.events.on(event, () => {
			if (!context || changed) return;
			changed = setTimeout(() => { changed = undefined; if (context) void update(context); }, 1_000);
			changed.unref();
		});
	}
	pi.on("session_start", (_event, ctx) => start(ctx));
	pi.on("session_tree", (_event, ctx) => start(ctx));
	pi.on("session_before_switch", () => stop());
	pi.on("session_before_fork", () => stop());
	pi.on("session_shutdown", () => stop());
}
