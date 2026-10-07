import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type Question = { id: number; text: string };
type State = { questions: Question[]; nextId: number };
const entryType = "follow-up-questions-state";

export default function (pi: ExtensionAPI) {
	let questions: Question[] = [];
	let nextId = 1;
	const snapshot = (): State => ({ questions: [...questions], nextId });
	const showCount = (ctx: ExtensionContext) => {
		if (ctx.mode === "tui") ctx.ui.setWidget(entryType, questions.length ?
			[`? ${questions.length} question${questions.length === 1 ? "" : "s"} · /questions to answer or clear`] : undefined);
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
		description: "Queue a concise, optional question for the user to answer later without blocking current work. Use when an answer would help but is not required to proceed safely. Do not use instead of asking immediately for required approval, spending, or consequential decisions. Use action=add with question; action=clear with id to withdraw a stale question, or omit id to clear all when the topic changes. The user can answer or clear questions with /questions.",
		parameters: Type.Object({
			action: Type.Union([Type.Literal("add"), Type.Literal("clear")]),
			question: Type.Optional(Type.String({ maxLength: 1000, description: "Question to queue (add only)" })),
			id: Type.Optional(Type.Integer({ minimum: 1, description: "Question ID to clear; omit to clear all" })),
		}),
		executionMode: "sequential",
		async execute(_id, params, _signal, _update, ctx) {
			let result: string;
			if (params.action === "add") {
				const text = params.question?.trim();
				if (!text || text.length > 1000) result = "Question rejected: provide nonempty text of at most 1000 characters.";
				else if (questions.length >= 20) result = "Question queue is full (20); clear stale questions first.";
				else {
					const item = { id: nextId++, text };
					questions = [...questions, item];
					result = `Queued #${item.id}: ${item.text}. The user can answer later with /questions.`;
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
			if (ctx.mode !== "tui") return;
			while (questions.length) {
				const choices = questions.map((item) => `#${item.id} ${item.text.replace(/\s+/g, " ")}`);
				choices.push("Clear all questions");
				const selected = await ctx.ui.select("Queued follow-up questions", choices);
				if (selected === undefined) return;
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
				const action = await ctx.ui.select(`#${item.id} ${item.text}`, ["Answer", "Clear this question", "Back"]);
				if (action === "Back" || action === undefined) continue;
				if (action === "Clear this question") {
					questions = questions.filter((q) => q.id !== item.id);
					pi.appendEntry(entryType, snapshot());
					showCount(ctx);
					continue;
				}
				const answer = (await ctx.ui.editor(`Answer: ${item.text}`))?.trim();
				if (!answer) continue;
				await pi.sendUserMessage(`> ${item.text.replace(/\n/g, "\n> ")}\n\n${answer}`, { deliverAs: "steer" });
				questions = questions.filter((q) => q.id !== item.id);
				pi.appendEntry(entryType, snapshot());
				showCount(ctx);
				ctx.ui.notify(`Answered question #${item.id}.`, "info");
				return;
			}
			ctx.ui.notify("No queued questions.", "info");
		},
	});
}
