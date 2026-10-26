import type { ExtensionContext } from "@earendil-works/pi-coding-agent";

type Theme = Pick<ExtensionContext["ui"]["theme"], "fg">;

const statusColors = {
	running: "mdLink", stopping: "warning", deadline_exceeded: "warning",
	succeeded: "success", failed: "error", timed_out: "error",
	cancelled: "muted", detached: "warning", unknown: "warning",
} as const;

// Presentation only: tool/status data remain plain strings.
export function jobStatusText(status: string, theme?: Theme, label = status): string {
	const color = statusColors[status as keyof typeof statusColors] ?? "warning";
	return theme?.fg?.(color, label) ?? label;
}

function counter(value: unknown): number | undefined {
	if (typeof value !== "number" && (typeof value !== "string" || !/^\d+$/.test(value))) return;
	if (value === "18446744073709551615") return; // systemd's UINT64_MAX unavailable marker.
	const number = Number(value);
	return Number.isFinite(number) && number >= 0 && number < 2 ** 64 - 1 ? number : undefined;
}
const decimal = (value: number) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(value);

export function humanBytes(value: unknown): string {
	let bytes = counter(value);
	if (bytes === undefined) return "unavailable";
	const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
	let unit = 0;
	while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit++; }
	return `${decimal(bytes)} ${units[unit]}`;
}

export function humanCpuTime(value: unknown): string {
	const nanoseconds = counter(value);
	if (nanoseconds === undefined) return "unavailable";
	const milliseconds = nanoseconds / 1_000_000;
	if (milliseconds < 1000) return `${decimal(milliseconds)} ms`;
	const seconds = milliseconds / 1000;
	if (seconds < 60) return `${decimal(seconds)} s`;
	if (seconds < 3600) return `${decimal(seconds / 60)} min`;
	return `${decimal(seconds / 3600)} h`;
}
