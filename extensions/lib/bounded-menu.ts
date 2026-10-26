import { stripVTControlCharacters } from "node:util";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { getKeybindings, matchesKey, truncateToWidth, wrapTextWithAnsi, type KeybindingsManager, type TuiMouseEvent, type TuiMouseEventResult } from "@earendil-works/pi-tui";

export const MENU_CLOSE = "__menu_close__";
export type MenuOptions = {
	keys?: Pick<KeybindingsManager, "matches" | "getKeys">;
	theme?: Pick<ExtensionContext["ui"]["theme"], "fg">;
	shortcuts?: Record<string, string>;
	shortcutLabels?: Record<string, string>;
	selectedIndex?: number;
	signal?: AbortSignal;
};

// Rendering updates only local scroll geometry; no I/O occurs during resize.
export function boundedMenuComponent(title: string, choices: string[], body: string,
	rows: () => number, done: (value: string | undefined) => void, redraw: () => void, options: MenuOptions = {}) {
	const keys = options.keys ?? getKeybindings();
	let selected = Math.max(0, Math.min(choices.length - 1, options.selectedIndex ?? 0));
	let offset = 0, bodyPage = 1, listPage = 1, help = false;
	let listTop = 1, renderedStart = 0, renderedCount = 0, pressedIndex: number | undefined;
	// Preserve SGR styling; remove whole non-style terminal sequences, not just
	// their ESC byte (which previously exposed literal [38;2;...m payloads).
	const clean = (value: string) => value.split(/(\x1b\[[0-9;:]*m)/g).map(part =>
		/^\x1b\[[0-9;:]*m$/.test(part) ? part : stripVTControlCharacters(part).replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "")).join("");
	const color = (role: "accent" | "dim", value: string) => options.theme?.fg?.(role, value) ?? value;
	const shortcutRows = () => Object.entries(options.shortcuts ?? {}).map(([key, action]) => `${key}: ${options.shortcutLabels?.[key] ?? (action === "@selected" ? "open selected item" : action)}`);
	const helpText = () => [
		`${keys.getKeys("tui.select.up").join("/")}: previous item`,
		`${keys.getKeys("tui.select.down").join("/")}: next item`,
		`${keys.getKeys("tui.select.confirm").join("/")}: activate`,
		`${keys.getKeys("tui.select.cancel").join("/")}: back one level`,
		`${keys.getKeys("tui.select.pageUp").join("/")}/${keys.getKeys("tui.select.pageDown").join("/")}: page ${body ? "details" : "list"}`,
		"q: close menu", "?: toggle this key help", ...shortcutRows(),
	].join("\n");
	const component = {
		title, choices, body,
		// Small selection facade also supports offline command/UI fixtures.
		list: {
			setSelectedIndex(index: number) { selected = Math.max(0, Math.min(choices.length - 1, index)); },
			getSelectedItem() { return choices[selected] === undefined ? null : { value: choices[selected], label: choices[selected] }; },
		},
		render(width: number): string[] {
			if (width < 1) return [];
			const detail = help ? helpText() : body;
			const height = Math.max(4, Math.floor(rows()) - 4);
			listPage = Math.min(choices.length, Math.max(1, Math.min(10, height - (detail ? 3 : 2))));
			bodyPage = Math.max(0, height - listPage - 2);
			const wrapped = detail ? clean(detail).split("\n").flatMap(line => wrapTextWithAnsi(line || " ", width)) : [];
			offset = Math.min(offset, Math.max(0, wrapped.length - bodyPage));
			const start = Math.max(0, Math.min(selected - Math.floor(listPage / 2), choices.length - listPage));
			const lines = [color("accent", `${clean(title).replace(/\s+/g, " ")} · ? keys`)];
			if (bodyPage && wrapped.length) lines.push(...wrapped.slice(offset, offset + bodyPage));
			listTop = lines.length; renderedStart = start; renderedCount = listPage;
			for (let i = start; i < start + listPage; i++) lines.push(`${i === selected ? color("accent", ">") : " "} ${clean(choices[i]).replace(/\s+/g, " ")}`);
			lines.push(color("dim", `${keys.getKeys("tui.select.cancel")[0] ?? "cancel"} back · q close · ${choices.length ? selected + 1 : 0}/${choices.length}${wrapped.length ? ` · details ${offset + 1}/${wrapped.length}` : ""}`));
			return lines.map(line => truncateToWidth(line, width));
		},
		handleInput(data: string) {
			// Configured navigation wins over mnemonic shortcuts (including remaps).
			if (keys.matches(data, "tui.select.cancel")) { done(undefined); return; }
			if (keys.matches(data, "tui.select.confirm")) { done(choices[selected]); return; }
			if (keys.matches(data, "tui.select.up")) selected = Math.max(0, selected - 1);
			else if (keys.matches(data, "tui.select.down")) selected = Math.min(choices.length - 1, selected + 1);
			else if (keys.matches(data, "tui.select.pageUp")) {
				if (body || help) offset = Math.max(0, offset - Math.max(1, bodyPage));
				else selected = Math.max(0, selected - Math.max(1, listPage));
			} else if (keys.matches(data, "tui.select.pageDown")) {
				if (body || help) offset += Math.max(1, bodyPage);
				else selected = Math.min(choices.length - 1, selected + Math.max(1, listPage));
			} else if (matchesKey(data, "q")) { done(MENU_CLOSE); return; }
			else if (data === "?") { help = !help; offset = 0; }
			else {
				for (const [key, action] of Object.entries(options.shortcuts ?? {})) {
					if (matchesKey(data, key as Parameters<typeof matchesKey>[1])) { done(action === "@selected" ? choices[selected] : action); return; }
				}
			}
			redraw();
		},
		handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
			if (event.type === "wheel" && event.wheelDelta) {
				const delta = event.wheelDelta < 0 ? -1 : 1;
				if ((body || help) && event.y > 0 && event.y < listTop) offset = Math.max(0, offset + delta * 3);
				else selected = Math.max(0, Math.min(choices.length - 1, selected + delta));
				redraw(); return { handled: true, render: true };
			}
			if (event.button !== "left" || (event.type !== "press" && event.type !== "click")) return;
			const index = renderedStart + event.y - listTop;
			if (event.y < listTop || event.y >= listTop + renderedCount || !choices[index]) return;
			if (event.type === "press") { pressedIndex = index; selected = index; redraw(); return { handled: true, render: true, focus: true }; }
			selected = pressedIndex ?? index; pressedIndex = undefined;
			done(choices[selected]); return { handled: true };
		},
		invalidate() {},
	};
	return component;
}

export function boundedMenu(ctx: ExtensionContext, title: string, choices: string[], body = "", options: MenuOptions = {}) {
	return ctx.ui.custom<string | undefined>((tui, theme, keys, done) => {
		const view = boundedMenuComponent(title, choices, body, () => tui.terminal.rows, done, () => tui.requestRender(), { ...options, theme: options.theme ?? theme, keys: typeof keys?.matches === "function" ? keys : getKeybindings() });
		const abort = () => done(undefined);
		options.signal?.addEventListener("abort", abort, { once: true });
		if (options.signal?.aborted) abort();
		return Object.assign(view, { dispose() { options.signal?.removeEventListener("abort", abort); } });
	});
}
