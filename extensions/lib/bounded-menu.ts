import { stripVTControlCharacters } from "node:util";
import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { getKeybindings, matchesKey, SelectList, truncateToWidth, wrapTextWithAnsi, type TuiMouseEvent, type TuiMouseEventResult } from "@earendil-works/pi-tui";

export const MENU_CLOSE = "__menu_close__";
export type MenuOptions = {
	theme?: Pick<ExtensionContext["ui"]["theme"], "fg">;
	shortcuts?: Record<string, string>;
	shortcutLabels?: Record<string, string>;
	selectedIndex?: number;
	signal?: AbortSignal;
};

// The shell budgets title/details/list/footer rows. Pi owns list rendering,
// selection (including wrapping), configured navigation and pointer behavior.
export function boundedMenuComponent(title: string, choices: string[], body: string,
	rows: () => number, done: (value: string | undefined) => void, redraw: () => void, options: MenuOptions = {}) {
	const keys = getKeybindings();
	let offset = 0, bodyPage = 1, listPage = 10, help = false;
	let listTop = 1, listHeight = 0;
	// Preserve SGR styling; remove whole non-style terminal sequences, not just
	// their ESC byte (which previously exposed literal [38;2;...m payloads).
	const clean = (value: string) => value.split(/(\x1b\[[0-9;:]*m)/g).map(part =>
		/^\x1b\[[0-9;:]*m$/.test(part) ? part : stripVTControlCharacters(part).replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "")).join("");
	const color = (role: "accent" | "dim", value: string) => options.theme?.fg?.(role, value) ?? value;
	const items = choices.map(value => ({ value, label: clean(value).replace(/\s+/g, " ") || " " }));
	const createList = (maxVisible: number, index: number) => {
		const next = new SelectList(items, maxVisible, {
			selectedPrefix: value => color("accent", value), selectedText: value => color("accent", value),
			description: value => color("dim", value), scrollInfo: value => color("dim", value), noMatch: value => color("dim", value),
		});
		next.setSelectedIndex(index);
		next.onSelect = item => done(item.value);
		next.onCancel = () => done(undefined);
		next.onSelectionChange = redraw;
		return next;
	};
	let list = createList(listPage, options.selectedIndex ?? 0);
	// Item identity, rather than its value, preserves duplicate-label positions.
	const selectedIndex = () => { const item = list.getSelectedItem(); return item ? items.indexOf(item) : 0; };
	const shortcutRows = () => Object.entries(options.shortcuts ?? {}).map(([key, action]) => `${key}: ${options.shortcutLabels?.[key] ?? (action === "@selected" ? "open selected item" : action)}`);
	const helpText = () => [
		`${keys.getKeys("tui.select.up").join("/")}: previous item (wraps)`,
		`${keys.getKeys("tui.select.down").join("/")}: next item (wraps)`,
		`${keys.getKeys("tui.select.confirm").join("/")}: activate`,
		`${keys.getKeys("tui.select.cancel").join("/")}: back one level`,
		`${keys.getKeys("tui.select.pageUp").join("/")}/${keys.getKeys("tui.select.pageDown").join("/")}: page ${body || help ? "details" : "list"}`,
		"q: close menu", "?: toggle this key help", ...shortcutRows(),
	].join("\n");
	return {
		title, choices, body,
		get list() { return list; },
		render(width: number): string[] {
			if (width < 1) return [];
			const detail = help ? helpText() : body;
			const height = Math.max(4, Math.floor(rows()) - 4);
			const listSpace = Math.max(2, height - (detail ? 3 : 2));
			// SelectList adds one scroll-indicator row when not all items fit.
			const visible = Math.min(10, Math.max(1, listSpace - (items.length > Math.min(10, listSpace) ? 1 : 0)));
			if (visible !== listPage) {
				const index = selectedIndex();
				listPage = visible;
				// Pi exposes maxVisible in the constructor, not a resize setter.
				list = createList(listPage, index);
			}
			const listLines = list.render(width);
			bodyPage = Math.max(0, height - listLines.length - 2);
			const wrapped = detail ? clean(detail).split("\n").flatMap(line => wrapTextWithAnsi(line || " ", width)) : [];
			offset = Math.min(offset, Math.max(0, wrapped.length - Math.max(1, bodyPage)));
			const lines = [color("accent", `${clean(title).replace(/\s+/g, " ")} · ? keys`)];
			if (bodyPage && wrapped.length) lines.push(...wrapped.slice(offset, offset + bodyPage));
			listTop = lines.length; listHeight = listLines.length;
			lines.push(...listLines);
			// Pi's list owns its position indicator; don't duplicate it here.
			lines.push(color("dim", `${keys.getKeys("tui.select.cancel")[0] ?? "cancel"} back · q close${wrapped.length ? ` · details ${offset + 1}/${wrapped.length}` : ""}`));
			return lines.map(line => truncateToWidth(line, width));
		},
		handleInput(data: string) {
			// Use the same host registry as SelectList; navigation beats mnemonics.
			if ((["tui.select.up", "tui.select.down", "tui.select.confirm", "tui.select.cancel"] as const).some(key => keys.matches(data, key))) {
				list.handleInput(data); return;
			}
			if (keys.matches(data, "tui.select.pageUp")) {
				if (body || help) offset = Math.max(0, offset - Math.max(1, bodyPage));
				else list.setSelectedIndex(selectedIndex() - listPage);
			} else if (keys.matches(data, "tui.select.pageDown")) {
				if (body || help) offset += Math.max(1, bodyPage);
				else list.setSelectedIndex(selectedIndex() + listPage);
			} else if (matchesKey(data, "q")) { done(MENU_CLOSE); return; }
			else if (data === "?") { help = !help; offset = 0; }
			else {
				for (const [key, action] of Object.entries(options.shortcuts ?? {})) {
					if (matchesKey(data, key as Parameters<typeof matchesKey>[1])) { done(action === "@selected" ? list.getSelectedItem()?.value : action); return; }
				}
			}
			redraw();
		},
		handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
			if (event.type === "wheel" && event.wheelDelta && event.y > 0 && event.y < listTop) {
				offset = Math.max(0, offset + (event.wheelDelta < 0 ? -3 : 3));
				redraw(); return { handled: true, render: true };
			}
			if (event.y < listTop || event.y >= listTop + listHeight) return;
			return list.handleMouse({ ...event, y: event.y - listTop });
		},
		invalidate() { list.invalidate(); },
	};
}

export function boundedMenu(ctx: ExtensionContext, title: string, choices: string[], body = "", options: MenuOptions = {}) {
	return ctx.ui.custom<string | undefined>((tui, theme, _keys, done) => {
		const view = boundedMenuComponent(title, choices, body, () => tui.terminal.rows, done, () => tui.requestRender(), { ...options, theme: options.theme ?? theme });
		const abort = () => done(undefined);
		options.signal?.addEventListener("abort", abort, { once: true });
		if (options.signal?.aborted) abort();
		return Object.assign(view, { dispose() { options.signal?.removeEventListener("abort", abort); } });
	});
}
