import { boundedMenu, MENU_CLOSE } from "./lib/bounded-menu.js";
import { createLocalBashOperations, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type Proposal = { command: string; reason: string };
type ProposalDetails = { proposals: Proposal[] };

/** Add reviewed suggestions and follow-up to Pi's built-in ! shell execution. */
export default function (pi: ExtensionAPI) {
	const commands: string[] = [];
	let proposals: Proposal[] = [];
	let followUp = true;
	const remember = (command: string) => {
		if (!command.trim() || commands.includes(command)) return;
		commands.unshift(command);
		if (commands.length > 100) commands.pop();
	};
	const restore = (ctx: ExtensionContext) => {
		commands.length = 0;
		proposals = [];
		for (const entry of ctx.sessionManager.getBranch()) {
			if (entry.type !== "message") continue;
			const message = entry.message;
			if (message.role === "bashExecution" && !message.excludeFromContext) remember(message.command);
			if (message.role === "toolResult" && message.toolName === "suggest_shell_command") {
				const details = message.details as ProposalDetails | undefined;
				if (details?.proposals) proposals = details.proposals;
			}
		}
	};

	pi.on("session_start", (_event, ctx) => {
		restore(ctx);
		if (ctx.mode !== "tui") return;
		ctx.ui.addAutocompleteProvider((current) => ({
			async getSuggestions(lines, cursorLine, cursorCol, options) {
				const line = lines[0] ?? "";
				if (cursorLine !== 0 || lines.length !== 1 || !line.startsWith("!") || line.startsWith("!!")) {
					return current.getSuggestions(lines, cursorLine, cursorCol, options);
				}
				const prefix = line.slice(1, cursorCol);
				if (!prefix.trim()) return current.getSuggestions(lines, cursorLine, cursorCol, options);
				const matches = commands.filter((command) => command.startsWith(prefix.trimStart())).slice(0, 10);
				if (!matches.length) return current.getSuggestions(lines, cursorLine, cursorCol, options);
				return { items: matches.map((command) => ({ value: command, label: `!${command}` })), prefix: `!${prefix}` };
			},
			applyCompletion(lines, cursorLine, cursorCol, item, prefix) {
				if (!prefix.startsWith("!")) return current.applyCompletion(lines, cursorLine, cursorCol, item, prefix);
				const completed = `!${lines[0].slice(1).match(/^\s*/)?.[0] ?? ""}${item.value}`;
				return { lines: [completed + lines[0].slice(cursorCol)], cursorLine: 0, cursorCol: completed.length };
			},
			shouldTriggerFileCompletion(lines, cursorLine, cursorCol) {
				return current.shouldTriggerFileCompletion?.(lines, cursorLine, cursorCol) ?? true;
			},
		}));
	});
	pi.on("session_tree", (_event, ctx) => restore(ctx));

	pi.registerCommand("shell-followup", {
		description: "Turn automatic agent follow-up after ! commands on or off (default: on)",
		handler: async (args, ctx) => {
			const setting = args.trim().toLowerCase();
			if (setting === "on") followUp = true;
			else if (setting === "off") followUp = false;
			else {
				ctx.ui.notify(`Shell follow-up is ${followUp ? "on" : "off"}. Use /shell-followup on or off.`, "info");
				return;
			}
			ctx.ui.notify(`Shell follow-up ${setting}.`, "info");
		},
	});

	pi.registerTool({
		name: "suggest_shell_command",
		label: "Suggest shell command",
		description: "Propose a shell command for the user to review and optionally insert. Does not run it. Use only when a shell command would help the user.",
		parameters: Type.Object({
			command: Type.String({ maxLength: 4096, description: "A single shell command, without a leading !" }),
			reason: Type.String({ maxLength: 500, description: "Why this command would help" }),
		}),
		executionMode: "sequential",
		async execute(_id, params) {
			const command = params.command.trim();
			const reason = params.reason.trim();
			if (!command || !reason || command.startsWith("!") || command.length > 4096 || reason.length > 500 ||
				/[\x00-\x1f\x7f]/.test(command + reason)) {
				return { content: [{ type: "text", text: "Proposal rejected: provide one single-line command (without ! or control characters) and a single-line reason." }], details: { proposals: [...proposals] } };
			}
			proposals = [{ command, reason }, ...proposals.filter((item) => item.command !== command)].slice(0, 20);
			return {
				content: [{ type: "text", text: `Proposed: ${command}\nReason: ${reason}\nNot executed. The user can review it with /shell-suggestions.` }],
				details: { proposals: [...proposals] } satisfies ProposalDetails,
			};
		},
	});

	pi.registerCommand("shell-suggestions", {
		description: "Review model-proposed shell commands and insert one into the editor (does not execute)",
		handler: async (_args, ctx) => {
			if (ctx.mode !== "tui") { ctx.ui.notify("/shell-suggestions requires the interactive terminal UI.", "warning"); return; }
			if (!proposals.length) {
				ctx.ui.notify("No shell command suggestions yet.", "info");
				return;
			}
			const choices = proposals.map((item, index) => `${index + 1}. ${item.command} — ${item.reason}`);
			const selected = await boundedMenu(ctx, "Shell suggestions (no command runs here)", choices, "", { shortcuts: { i: "@selected" }, shortcutLabels: { i: "review/insert selected command" } });
			if (selected === undefined || selected === MENU_CLOSE) return;
			const proposal = proposals[choices.indexOf(selected)];
			if (!proposal) return;
			if (ctx.ui.getEditorText() && !(await ctx.ui.confirm("Replace current draft?", "This will replace your unsent editor text."))) return;
			if (!(await ctx.ui.confirm("Insert shell command?", `${proposal.command}\n\nReason: ${proposal.reason}\n\nThis only fills the editor. Press Enter to run it.`))) return;
			ctx.ui.setEditorText(`! ${proposal.command}`);
		},
	});

	pi.on("user_bash", (event, ctx) => {
		if (event.excludeFromContext) return; // Pi's !! command is private and never starts a turn.
		remember(event.command);
		if (!followUp || !ctx.isIdle() || ctx.hasPendingMessages()) return;

		const previousLeaf = ctx.sessionManager.getLeafId();
		const local = createLocalBashOperations({ shellPath: pi.getSettings().shellPath });
		return {
			operations: {
				async exec(command, cwd, options) {
					const result = await local.exec(command, cwd, options);
					// Pi records the bashExecution entry after exec resolves; the next event-loop turn sees it.
					setImmediate(() => {
						const last = ctx.sessionManager.getBranch().at(-1);
						if (!followUp || !ctx.isIdle() || ctx.hasPendingMessages() || last?.type !== "message" ||
							last.parentId !== previousLeaf || last.message.role !== "bashExecution" ||
							last.message.command !== event.command || last.message.cancelled) return;
						pi.sendUserMessage("Please review the immediately preceding shell command and its output in the session, then continue helping. Do not rerun the command just to see its result.");
					});
					return result;
				},
			},
		};
	});
}
