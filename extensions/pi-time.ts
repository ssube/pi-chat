import { setTimeout as delay } from "node:timers/promises";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MAX_TIMER_MS = 2_147_483_647;
const units: Record<string, number> = {
  second: 1_000, minute: 60_000, hour: 3_600_000,
  day: 86_400_000, week: 604_800_000,
};

function targetTime(input: string, now: number): number {
  const interval = /^(\d+(?:\.\d+)?)\s+(seconds?|minutes?|hours?|days?|weeks?)$/i.exec(input);
  if (interval) {
    const unit = interval[2].toLowerCase().replace(/s$/, "");
    const milliseconds = Number(interval[1]) * units[unit];
    if (!Number.isSafeInteger(milliseconds) || milliseconds < 1) throw new Error("Interval must be at least 1 millisecond and have an exact millisecond duration.");
    const target = now + milliseconds;
    if (!Number.isSafeInteger(target) || target > 8_640_000_000_000_000) throw new Error("Target time is out of range.");
    return target;
  }

  // An explicit offset is required; a bare ISO local time depends on the host's timezone.
  const iso = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/i.exec(input);
  if (!iso) throw new Error("Use an interval such as '3 hours' or an ISO time with timezone, such as '2026-10-08T12:00:00Z'.");
  const [, year, month, day, hour, minute, second, zone] = iso;
  const offset = zone.toUpperCase() === "Z" ? 0 : (zone[0] === "+" ? 1 : -1) * (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(4, 6)));
  if (zone.toUpperCase() !== "Z" && (Number(zone.slice(1, 3)) > 23 || Number(zone.slice(4, 6)) > 59)) throw new Error("Invalid ISO timezone offset.");
  const target = Date.parse(input);
  const wall = new Date(target + offset * 60_000);
  if (!Number.isFinite(target) || wall.getUTCFullYear() !== Number(year) || wall.getUTCMonth() + 1 !== Number(month) ||
      wall.getUTCDate() !== Number(day) || wall.getUTCHours() !== Number(hour) || wall.getUTCMinutes() !== Number(minute) ||
      wall.getUTCSeconds() !== Number(second)) throw new Error("Invalid ISO date-time.");
  return target;
}

function humanInterval(milliseconds: number): string {
  const parts: string[] = [];
  for (const [unit, size] of Object.entries(units).reverse().concat([["millisecond", 1]])) {
    const count = Math.floor(milliseconds / size);
    if (count) parts.push(`${count} ${unit}${count === 1 ? "" : "s"}`);
    milliseconds %= size;
  }
  return parts.join(", ") || "0 seconds";
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "clock",
    label: "Clock",
    description: "Get the current time as epoch milliseconds, ISO UTC, human-readable UTC, or human-readable host-local time. Select the output with mode.",
    parameters: Type.Object({ mode: Type.Union([
      Type.Literal("epoch_ms"), Type.Literal("iso_utc"), Type.Literal("human_utc"), Type.Literal("human_local"),
    ]) }),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    async execute(_id, { mode }) {
      const now = new Date();
      let value: string | number;
      if (mode === "epoch_ms") value = now.getTime();
      else if (mode === "iso_utc") value = now.toISOString();
      else {
        const utc = mode === "human_utc";
        const zone = utc ? "UTC" : Intl.DateTimeFormat().resolvedOptions().timeZone;
        const offset = -now.getTimezoneOffset();
        const sign = offset >= 0 ? "+" : "-";
        const hh = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
        const mm = String(Math.abs(offset) % 60).padStart(2, "0");
        const date = new Intl.DateTimeFormat("en-US", {
          timeZone: utc ? "UTC" : undefined, dateStyle: "full", timeStyle: "long",
        }).format(now);
        value = utc ? date : `${date} (${zone}, UTC${sign}${hh}:${mm})`;
      }
      return { content: [{ type: "text", text: String(value) }], details: { mode, value } };
    },
  });

  pi.registerTool({
    name: "sleep_until",
    label: "Sleep until",
    description: "Wait until a target ISO date-time with explicit timezone (Z or ±HH:MM), or for a positive interval such as '3 hours'. Supports seconds, minutes, hours, days, and weeks. The wait is cancellable; a time already passed returns immediately. Use only for fixed time-based delays, not to monitor asynchronous results: use Forgejo wait for CI jobs, Matrix wait for Matrix messages, and Discord read/poll for Discord messages.",
    parameters: Type.Object({ time: Type.String({ description: "Positive interval (e.g. '3 hours') or ISO date-time with explicit timezone" }) }),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    async execute(_id, { time }, signal, onUpdate, ctx) {
      const now = Date.now();
      const target = targetTime(time.trim(), now);
      if (signal?.aborted) throw signal.reason ?? new Error("Wait cancelled.");
      const durationMs = Math.max(0, target - now);
      const interval = humanInterval(durationMs);
      const expected = new Date(target).toISOString();
      const waiting = `Waiting ${interval}; expected wake: ${expected}.`;
      if (ctx?.hasUI) ctx.ui.notify(waiting, "info");
      onUpdate?.({ content: [{ type: "text", text: waiting }], details: { target: expected, durationMs } });
      while (target > Date.now()) {
        await delay(Math.min(target - Date.now(), MAX_TIMER_MS), undefined, { signal });
      }
      const details = { target: expected, wokeAt: new Date().toISOString(), durationMs };
      return { content: [{ type: "text", text: `Woke at ${details.wokeAt}; expected wake: ${expected}; interval: ${interval}.` }], details };
    },
  });
}
