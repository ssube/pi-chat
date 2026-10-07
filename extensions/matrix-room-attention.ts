// Optional room labels for the interactive room-lease extension.
// Install the room-lease extension separately; keep the bridge socket in the launcher.
import { readFileSync, statSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	pi.on("session_start", (_event, ctx) => {
		if (ctx.mode !== "tui" || process.env.AGENT_BRIDGE_INVOCATION) return;
		const file = process.env.PI_ROOM_LEASE_LABEL_FILE;
		if (!file) return;
		const info = statSync(file);
		if (!info.isFile() || info.uid !== process.getuid!() || (info.mode & 0o077)) {
			throw new Error("room label file must be an owner-only regular file");
		}
		const room = JSON.parse(readFileSync(file, "utf8")) as { room_id?: string; name?: string };
		if (!room.room_id?.startsWith("!") || !room.room_id.includes(":") ||
			!room.name || room.name.length > 80 || /[\r\n]/.test(room.name)) {
			throw new Error("invalid room label");
		}
		process.env.PI_ROOM_LEASE_LABEL_ROOM = room.room_id;
		process.env.PI_ROOM_LEASE_LABEL_NAME = room.name;
	});
}
