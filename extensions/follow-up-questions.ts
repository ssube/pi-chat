import { boundedMenu, MENU_CLOSE } from "./lib/bounded-menu.js";
import { stableWidget } from "./lib/stable-widget.js";
import { attentionBell } from "./lib/attention-bell.js";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type Question = { id: number; text: string; context?: string; task_id?: number; wait_for_answer?: boolean };
type State = { questions: Question[]; nextId: number };
const entryType = "follow-up-questions-state";

export default function (pi: ExtensionAPI) {
	let questions: Question[] = [];
	let nextId = 1;
	const snapshot = (): State => ({ questions: [...questions], nextId });
	const taskAvailable = (task_id: number) => {
		const lookup: { task_id: number; available?: boolean } = { task_id };
		pi.events.emit("tasks:lookup-v1", lookup);
		return lookup.available === true;
	};
	// The queue is the sole owner of unanswered links. Task views query it synchronously.
	pi.events.on("questions:lookup-v1", payload => {
		if (!payload || typeof payload !== "object") return;
		const query = payload as { task_id?: unknown; ids?: number[]; waiting_ids?: number[] };
		if (typeof query.task_id !== "number" || !Number.isSafeInteger(query.task_id) || query.task_id < 1) return;
		const linked = questions.filter(item => item.task_id === query.task_id);
		query.ids = linked.map(item => item.id);
		query.waiting_ids = linked.filter(item => item.wait_for_answer === true).map(item => item.id);
	});
	const mountQuestions = stableWidget(entryType, "/questions");
	const showCount = (ctx: ExtensionContext) => {
		mountQuestions(ctx, questions.length ? () =>
			[`? ${questions.length} question${questions.length === 1 ? "" : "s"} · /questions to answer or clear`] : undefined);
		pi.events.emit("questions:changed-v1", {});
	};
	const restore = (ctx: ExtensionContext) => {
		questions = [];
		nextId = 1;
		for (const entry of ctx.sessionManager.getBranch()) {
			if (entry.type === "custom" && entry.customType === entryType) {
				const state = entry.data as State;
				questions = state.questions;
				nextId = state.nextId;
			} else if (entry.type === "message" && entry.message.role === "toolResult" &&
				entry.message.toolName === "queue_question") {
				const state = entry.message.details as State | undefined;
				if (state) {
					questions = state.questions;
					nextId = state.nextId;
				}
			}
		}
		showCount(ctx);
	};
	pi.on("session_start", (_event, ctx) => restore(ctx));
	pi.on("session_tree", (_event, ctx) => restore(ctx));

	pi.registerTool({
		name: "queue_question",
		label: "Queue follow-up question",
		description: "Queue a concise, optional question for the user to answer later without blocking current work. Supply task_id for a linked open task; invalid, completed or unavailable links are refused. Set wait_for_answer=true only when that task cannot proceed without the answer: it gives the task a derived question state and suppresses its automatic reminders until answer or clear. A progress task still holds its slot. Do not queue required approval, spending, or consequential decisions; ask directly. Use action=clear to withdraw one id or all. The user can answer or clear questions with /questions.",
		parameters: Type.Object({
			action: Type.Union([Type.Literal("add"), Type.Literal("clear")]),
			question: Type.Optional(Type.String({ maxLength: 1000, description: "Question to queue (add only)" })),
			context: Type.Optional(Type.String({ maxLength: 2000, description: "Brief context shown with the question: why it matters and relevant background (add only)" })),
			task_id: Type.Optional(Type.Integer({ minimum: 1, description: "Open task ID to link (add only)" })),
			wait_for_answer: Type.Optional(Type.Boolean({ description: "Linked task cannot proceed without this answer (add only; default false)" })),
			id: Type.Optional(Type.Integer({ minimum: 1, description: "Question ID to clear; omit to clear all" })),
		}),
		executionMode: "sequential",
		async execute(_id, params, _signal, _update, ctx) {
			let result: string;
			if (params.action === "add") {
				const text = params.question?.trim();
				const context = params.context?.trim();
				if (!text || text.length > 1000) result = "Question rejected: provide nonempty text of at most 1000 characters.";
				else if (context && context.length > 2000) result = "Question rejected: context must be at most 2000 characters.";
				else if (questions.length >= 20) result = "Question queue is full (20); clear stale questions first.";
				else if (params.wait_for_answer !== undefined && typeof params.wait_for_answer !== "boolean") result = "Question rejected: wait_for_answer must be boolean.";
				else if (params.wait_for_answer === true && params.task_id === undefined) result = "Question rejected: wait_for_answer requires task_id.";
				else if (params.task_id !== undefined && (!Number.isSafeInteger(params.task_id) || !taskAvailable(params.task_id))) {
					result = `Question rejected: task #${params.task_id} is not open in this session.`;
				} else {
					const item: Question = { id: nextId++, text };
					if (context) item.context = context;
					if (params.task_id !== undefined) item.task_id = params.task_id;
					if (params.wait_for_answer === true) item.wait_for_answer = true;
					questions = [...questions, item];
					attentionBell(ctx);
					result = `Queued #${item.id}${item.task_id ? ` for task #${item.task_id}` : ""}${item.wait_for_answer ? " (waiting for answer)" : ""}: ${item.text}. The user can answer later with /questions.`;
				}
			} else if (params.id === undefined) {
				result = `Cleared ${questions.length} queued questions.`;
				questions = [];
			} else {
				const before = questions.length;
				questions = questions.filter((item) => item.id !== params.id);
				result = before === questions.length ? `Question #${params.id} not found.` : `Cleared question #${params.id}.`;
			}
			showCount(ctx);
			return { content: [{ type: "text", text: result }], details: snapshot() };
		},
	});

	pi.registerCommand("questions", {
		description: "Answer or clear queued follow-up questions",
		handler: async (_args, ctx) => {
			if (ctx.mode !== "tui") { ctx.ui.notify("/questions requires the interactive terminal UI.", "warning"); return; }
			while (questions.length) {
				const choices = questions.map((item) => `#${item.id}${item.task_id ? ` [task #${item.task_id}]` : ""}${item.wait_for_answer ? " [waiting]" : ""} ${item.text.replace(/\s+/g, " ")}`);
				choices.push("Clear all questions");
				const selected = await boundedMenu(ctx, "Queued follow-up questions", choices);
				if (selected === undefined || selected === MENU_CLOSE) return;
				if (selected === "Clear all questions") {
					if (await ctx.ui.confirm("Clear all questions?", `Remove ${questions.length} pending questions?`)) {
						questions = [];
						pi.appendEntry(entryType, snapshot());
						showCount(ctx);
					}
					return;
				}
				const item = questions[choices.indexOf(selected)];
				if (!item) return;
				const heading = `#${item.id}${item.task_id ? ` [task #${item.task_id}]` : ""} ${item.text}${item.context ? `\n\n${item.context}` : ""}`;
				const action = await boundedMenu(ctx, `Question #${item.id}`, ["Answer", "Clear this question", "Back"], heading, { shortcuts: { a: "Answer", x: "Clear this question" } });
				if (action === MENU_CLOSE) return;
				if (action === "Back" || action === undefined) continue;
				if (action === "Clear this question") {
					questions = questions.filter((q) => q.id !== item.id);
					pi.appendEntry(entryType, snapshot());
					showCount(ctx);
					continue;
				}
				const answer = (await ctx.ui.editor(`Answer question #${item.id}`))?.trim();
				if (!answer) continue;
				const previous = questions;
				questions = questions.filter((q) => q.id !== item.id);
				try {
					pi.appendEntry(entryType, snapshot());
					showCount(ctx);
					// Pi's extension sendUserMessage is fire-and-forget. Clear first so its next turn sees no stale question overlay.
					pi.sendUserMessage(`${item.task_id ? `Task #${item.task_id} — queued question\n\n` : ""}> ${item.text.replace(/\n/g, "\n> ")}\n\n${answer}`, { deliverAs: "steer" });
				} catch {
					questions = previous;
					try { pi.appendEntry(entryType, snapshot()); showCount(ctx); ctx.ui.notify("Answer was not queued; question restored.", "error"); }
					catch { ctx.ui.notify("Answer was not queued and queue recovery could not be saved.", "error"); }
					return;
				}
				ctx.ui.notify(`Answered question #${item.id}.`, "info");
				return;
			}
			ctx.ui.notify("No queued questions.", "info");
		},
	});
}
