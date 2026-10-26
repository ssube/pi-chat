import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { truncateToWidth } from "@earendil-works/pi-tui";

// Update a mounted component instead of deleting/reinserting its widget key.
export function stableWidget(key: string, command: string) {
	let owner: ExtensionContext["ui"] | undefined;
	let content: ((width: number) => string[]) | undefined;
	let redraw: (() => void) | undefined;
	return (ctx: ExtensionContext, next?: (width: number) => string[]) => {
		if (ctx.mode !== "tui") return;
		content = next;
		if (!next) {
			ctx.ui.setWidget(key, undefined); owner = undefined; redraw = undefined; return;
		}
		if (owner === ctx.ui) { redraw?.(); return; }
		owner = ctx.ui;
		ctx.ui.setWidget(key, tui => {
			redraw = () => tui.requestRender();
			return {
				render(width: number) {
					if (width < 1) return [];
					const contentWidth = Math.max(0, width - 1);
					const rows = content?.(Math.max(1, contentWidth)) ?? [];
					const limit = Math.max(1, Math.min(8, Math.floor(tui.terminal.rows / 3)));
					const visible = rows.length > limit ? [...rows.slice(0, limit - 1), `… ${rows.length - limit + 1} more rows · ${command}`] : rows;
					// Match Pi's array-widget Text(line, 1, 0) inset without overflowing.
					return visible.map(row => " " + truncateToWidth(row, contentWidth));
				},
				invalidate() {},
			};
		});
	};
}
