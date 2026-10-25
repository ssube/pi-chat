import { randomUUID } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const requestEvent = "subagents:rpc:v1:request";
export type Assignment = {
	session_id: string; request_id: string; run_id?: string; agent: string;
	state: "launching" | "assigned" | "returned"; last_state?: string;
};
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const states = ["queued", "running", "complete", "failed", "partial", "paused", "stopped", "rejected"];
export const terminalRun = (state: string) => ["complete", "failed", "stopped", "rejected"].includes(state);
export const activeRun = (state: string) => state === "queued" || state === "running";

export function validateAssignment(value: Assignment) {
	if (!record(value) || typeof value.session_id !== "string" || !value.session_id || value.session_id.length > 4096 ||
		typeof value.request_id !== "string" || !/^[0-9a-f-]{36}$/.test(value.request_id) ||
		typeof value.agent !== "string" || !value.agent.trim() || value.agent.length > 120 ||
		(value.run_id !== undefined && (typeof value.run_id !== "string" || !value.run_id.trim() || value.run_id.length > 256)) ||
		!["launching", "assigned", "returned"].includes(value.state) ||
		(value.state !== "launching" && !value.run_id) ||
		(value.last_state !== undefined && !states.includes(value.last_state)) ||
		(value.state === "returned" && (!value.last_state || !terminalRun(value.last_state)))) throw new Error("Invalid saved subagent assignment.");
}

// No package dependency: independently installed Pi extensions share the public event protocol.
export function taskSubagents(pi: ExtensionAPI) {
	const rpc = (method: string, params: object = {}, requestId: string = randomUUID()): Promise<Record<string, unknown>> => new Promise((resolve, reject) => {
		let off: (() => void) | undefined;
		const timer = setTimeout(() => { off?.(); reject(new Error(`Subagents ${method} reply unavailable. A launch may still have started; do not retry it. Refresh the assignment to recover.`)); }, method === "spawn" ? 60_000 : method === "ping" ? 1500 : 10_000);
		off = pi.events.on(`subagents:rpc:v1:reply:${requestId}`, raw => {
			if (!record(raw) || raw.version !== 1 || raw.requestId !== requestId) return;
			clearTimeout(timer); off?.();
			if (raw.success === true && record(raw.data)) resolve(raw.data);
			else reject(new Error("Subagents RPC rejected the request; inspect the run through the plugin before retrying a launch."));
		});
		try { pi.events.emit(requestEvent, { version: 1, requestId, method, params, source: { extension: "tasks" } }); }
		catch (error) { clearTimeout(timer); off?.(); reject(error); }
	});
	return {
		async available(sessionId: string) {
			const data = await rpc("ping");
			if (!record(data.session) || data.session.sessionId !== sessionId || !record(data.capabilities) ||
				data.capabilities.asyncSpawn !== true || !record(data.capabilities.asyncStatusSnapshot) ||
				data.capabilities.asyncStatusSnapshot.version !== 1) throw new Error("Compatible current-session pi-subagents RPC is unavailable.");
		},
		async spawn(link: Assignment, task: string, cwd: string) {
			const data = await rpc("spawn", { agent: link.agent, task, cwd, context: "fresh" }, link.request_id);
			if (!record(data.details) || typeof data.details.runId !== "string" || !data.details.runId || data.details.runId.length > 256) throw new Error("Missing launch identity; refresh the assignment, do not launch again.");
			return data.details.runId;
		},
		async observe(link: Assignment) {
			const data = await rpc("status", { id: link.run_id ?? `rpc-spawn-${link.request_id}` });
			const id = link.run_id ?? (record(data.details) ? data.details.runId : undefined);
			const snapshot = data.asyncSnapshot;
			if (typeof id !== "string" || !record(snapshot) || snapshot.kind !== "pi-subagents.async-status-snapshot" || snapshot.version !== 1 || !Array.isArray(snapshot.runs)) return undefined;
			const run = snapshot.runs.find(item => record(item) && item.id === id);
			if (!record(run) || typeof run.state !== "string" || !states.includes(run.state)) return undefined;
			return { id, state: run.state, agent: typeof run.label === "string" && run.label.trim() && run.label.length <= 120 ? run.label : link.agent };
		},
	};
}
