// Interactive Pi inbox for Matrix messages forwarded by the local bridge.
// Install as a user extension and set AGENT_BRIDGE_ROOM_LEASE_SOCKET in the
// interactive Pi launcher to the owner's bridge-room-lease.sock path.
import { createServer, connect, type Server } from "node:net";
import { mkdtempSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

interface MatrixEvent {
  type?: string;
  event_id: string;
  sender: string;
  content: { body: string; msgtype: string; "m.relates_to"?: {
    rel_type?: string; event_id?: string; "m.in_reply_to"?: { event_id: string };
  }; "m.new_content"?: { body?: string; msgtype?: string } };
}
interface ReplyContext { event_id: string; sender?: string; body?: string }
interface LeaseResponse { ok: boolean; leases?: Array<{ room: string; seconds: number }>; released?: number }

export function formatMatrixMessage(room: string, event: MatrixEvent, reply?: ReplyContext,
                                    ping?: boolean, recipient?: string): string {
  const content = event.content;
  const relation = content["m.relates_to"];
  let body = content.body as string;
  // Do not present Matrix's plaintext reply fallback twice.
  if (relation?.["m.in_reply_to"] && body.startsWith("> ")) {
    const split = body.search(/\r?\n\r?\n/);
    const firstBreak = body.search(/\r?\n/);
    body = split >= 0 ? body.slice(split).replace(/^\r?\n\r?\n/, "")
      : firstBreak >= 0 ? body.slice(firstBreak).replace(/^\r?\n/, "") : "";
  }
  const thread = relation?.rel_type === "m.thread" ? ` · thread ${relation.event_id}` : "";
  const source = `Matrix · room ${room}${thread} · sender ${event.sender} · event ${event.event_id}`;
  let quoted = "";
  const parentId = relation?.["m.in_reply_to"]?.event_id;
  if (typeof parentId === "string" && parentId.startsWith("$")) {
    quoted = (reply?.event_id === parentId && typeof reply.sender === "string" && typeof reply.body === "string")
      ? `In reply to Matrix · room ${room} · sender ${reply.sender} · event ${parentId}:\n` +
        reply.body.slice(0, 1600).split(/\r?\n/).map((line: string) => `> ${line}`).join("\n") + "\n\n"
      : `In reply to Matrix · room ${room} · event ${parentId} (original unavailable).\n\n`;
  }
  const prefix = content.msgtype === "m.image" ? "[Image] " : "";
  const pingLine = ping === true && recipient ? `Ping: ${recipient}\n` : "";
  return `[${source}]\n${pingLine}\n${quoted}Message:\n${prefix}${body.slice(0, 8000)}`;
}

function bridgeRequest(path: string, request: object): Promise<LeaseResponse> {
  return new Promise((resolve, reject) => {
    const connection = connect(path, () => {
      connection.write(JSON.stringify(request) + "\n");
    });
    let data = "";
    connection.setTimeout(2500, () => connection.destroy(new Error("bridge timeout")));
    connection.on("data", (chunk: Buffer) => {
      data += chunk.toString("utf8");
      if (data.length > 16384) { connection.destroy(new Error("bridge response too large")); return; }
      if (data.includes("\n")) {
        connection.end();
        try { resolve(JSON.parse(data.split("\n", 1)[0])); }
        catch (error) { reject(error); }
      }
    });
    connection.once("error", reject);
    connection.once("close", () => { if (!data.includes("\n")) reject(new Error("bridge disconnected")); });
  });
}

export default function (pi: ExtensionAPI, editQuietMs = 60_000) {
  let server: Server | undefined;
  let bridgeSocketPath: string | undefined;
  let leaseSecret: string | undefined;
  let directory: string | undefined;
  let statusTimer: ReturnType<typeof setInterval> | undefined;
  const seen = new Set<string>();
  const pending = new Map<string, Promise<void>>();
  const pendingEdits = new Map<string, ReturnType<typeof setTimeout>>();
  let currentModel: ExtensionContext["model"];
  pi.on("model_select", (event) => { currentModel = event.model; });

  pi.on("session_start", async (_event, ctx) => {
    currentModel = ctx.model;
    if (ctx.mode !== "tui" || process.env.AGENT_BRIDGE_INVOCATION) return;
    const bridgeSocket = process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET;
    if (!bridgeSocket) return;
    // Reconstructed from the Pi transcript, not a second file of private posts.
    for (const entry of ctx.sessionManager?.getBranch() ?? []) {
      if (entry.type !== "message" || entry.message.role !== "user") continue;
      for (const part of entry.message.content) {
        if (part.type !== "text") continue;
        const match = part.text.match(/^\[Matrix · room .* · sender .* · event (\$[^\]\s]+)\]/);
        if (match) seen.add(match[1]);
      }
    }
    directory = mkdtempSync(join(tmpdir(), "pi-room-lease-"));
    chmodSync(directory, 0o700);
    const inbox = join(directory, "inbox.sock");
    const secret = randomBytes(32).toString("hex");
    server = createServer((connection) => {
      const chunks: Buffer[] = [];
      let size = 0;
      connection.setTimeout(20000, () => connection.destroy());
      connection.on("data", async (chunk: Buffer) => {
        chunks.push(chunk);
        size += chunk.length;
        if (size > 30_000_000) { connection.destroy(); return; }
        if (!chunk.includes(10)) return;
        try {
          const packet = JSON.parse(Buffer.concat(chunks).toString("utf8").split("\n", 1)[0]) as {
            secret?: string; room?: string; event?: MatrixEvent;
            image?: { type?: string; mimeType?: string; data?: string };
            reply?: ReplyContext; ping?: boolean; recipient?: string;
          };
          const event = packet.event;
          const content = event?.content;
          if (packet.secret !== secret || typeof packet.room !== "string" ||
              event?.type !== "m.room.message" || !["m.text", "m.image"].includes(content?.msgtype ?? "") ||
              typeof event.event_id !== "string" || typeof event.sender !== "string" ||
              typeof content?.body !== "string") {
            throw new Error("invalid room event");
          }
          if (seen.has(event.event_id)) {
            connection.end('{"ok":true}\n');
            return;
          }
          const relation = content["m.relates_to"];
          if (relation?.rel_type === "m.replace") {
            const original = relation.event_id;
            const edited = content["m.new_content"];
            if (typeof original !== "string" || !original.startsWith("$") ||
                typeof edited?.body !== "string" || !["m.text", "m.image"].includes(edited.msgtype ?? "")) {
              connection.end('{"ok":true}\n');
              return;
            }
            seen.add(event.event_id);
            const key = `${packet.room}\0${event.sender}\0${original}`;
            const previous = pendingEdits.get(key);
            if (previous) clearTimeout(previous);
            const message = `[Matrix edit · room ${packet.room} · sender ${event.sender} · edit event ${event.event_id} · original event ${original}]\n` +
              (packet.ping === true && packet.recipient ? `Ping: ${packet.recipient}\n` : "") +
              `\nEdited message:\n${edited.body.slice(0, 8000)}`;
            const timer = setTimeout(async () => {
              try {
                if (bridgeSocketPath !== bridgeSocket || leaseSecret !== secret) return;
                const status = await bridgeRequest(bridgeSocket, { op: "status", secret });
                if (status.ok !== true || !status.leases?.some(lease => lease.room === packet.room) ||
                    bridgeSocketPath !== bridgeSocket || leaseSecret !== secret ||
                    !currentModel || !(await ctx.modelRegistry.getApiKeyAndHeaders(currentModel)).ok ||
                    pendingEdits.get(key) !== timer) return;
                pi.sendUserMessage(message, ctx.isIdle() ? undefined : { deliverAs: "steer" });
              } catch { /* Ephemeral edit: a failed delivery is never replayed into another session. */ }
              finally { if (pendingEdits.get(key) === timer) pendingEdits.delete(key); }
            }, editQuietMs);
            timer.unref();
            pendingEdits.set(key, timer);
            connection.end('{"ok":true}\n');
            return;
          }
          let delivery = pending.get(event.event_id);
          if (!delivery) {
            delivery = (async () => {
              // Pi confirms enqueueing, not completion. Reject known prompt failures.
              if (!currentModel || !(await ctx.modelRegistry.getApiKeyAndHeaders(currentModel)).ok)
                throw new Error("no usable Pi model");
              const image = packet.image;
              const hasImage = content.msgtype === "m.image" && image?.type === "image" &&
                typeof image.mimeType === "string" && image.mimeType.startsWith("image/") &&
                typeof image.data === "string" && image.data.length <= 27_000_000;
              const message = formatMatrixMessage(packet.room, event, packet.reply,
                packet.ping, packet.recipient) +
                (content.msgtype === "m.image" && !hasImage ? "\n[Image unavailable; not attached]" : "");
              if (hasImage) {
                pi.sendUserMessage([{ type: "text", text: message },
                                    { type: "image", data: image.data, mimeType: image.mimeType }],
                                   ctx.isIdle() ? undefined : { deliverAs: "steer" });
              } else {
                pi.sendUserMessage(message, ctx.isIdle() ? undefined : { deliverAs: "steer" });
              }
              seen.add(event.event_id);
            })();
            pending.set(event.event_id, delivery);
            void delivery.then(() => pending.delete(event.event_id), () => pending.delete(event.event_id));
          }
          await delivery;
          connection.end('{"ok":true}\n');
        } catch {
          connection.end('{"ok":false}\n');
        }
      });
    });
    try {
      await new Promise<void>((resolve, reject) => {
        server!.once("error", reject);
        server!.listen(inbox, () => { server!.off("error", reject); resolve(); });
      });
      chmodSync(inbox, 0o600);
      process.env.PI_ROOM_LEASE_INBOX = inbox;
      process.env.PI_ROOM_LEASE_SECRET = secret;
      process.env.PI_ROOM_LEASE_BRIDGE_SOCKET = bridgeSocket;
      bridgeSocketPath = bridgeSocket;
      leaseSecret = secret;
      let owned = false;
      const poll = async () => {
        try {
          const result = await bridgeRequest(bridgeSocket, { op: "status", secret });
          if (result.ok !== true || !Array.isArray(result.leases)) throw new Error("bridge status unavailable");
          if (result.leases.length) {
            if (!result.leases.every(lease => Number.isFinite(lease.seconds) && lease.seconds >= 0))
              throw new Error("invalid lease seconds");
            owned = true;
            const seconds = Math.min(...result.leases.map((lease) => lease.seconds));
            const rooms = result.leases.map((lease) => lease.room === process.env.PI_ROOM_LEASE_LABEL_ROOM &&
              process.env.PI_ROOM_LEASE_LABEL_NAME
              ? `${process.env.PI_ROOM_LEASE_LABEL_NAME} (${lease.room})` : lease.room).join(", ");
            ctx.ui.setStatus("matrix-room-lease", `Matrix attention (${result.leases.length}): ${rooms} · ${seconds}s left`);
          } else {
            if (owned) ctx.ui.notify("Matrix attention ended; bridge Pi handles new room messages.", "warning");
            owned = false;
            ctx.ui.setStatus("matrix-room-lease", undefined);
          }
        } catch {
          if (owned) ctx.ui.notify("Matrix bridge connection lost; room ownership is uncertain.", "warning");
          owned = false;
          ctx.ui.setStatus("matrix-room-lease", undefined);
        }
      };
      statusTimer = setInterval(() => { void poll(); }, 5000);
      statusTimer.unref();
    } catch (error) {
      server.close();
      rmSync(directory, { recursive: true, force: true });
      server = undefined;
      directory = undefined;
      throw error;
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    for (const timer of pendingEdits.values()) clearTimeout(timer);
    pendingEdits.clear();
    if (bridgeSocketPath && leaseSecret) {
      try {
        await bridgeRequest(bridgeSocketPath, { op: "release_all", secret: leaseSecret });
      } catch { /* The bridge also drops the lease when inbox delivery fails. */ }
    }
    bridgeSocketPath = undefined;
    leaseSecret = undefined;
    currentModel = undefined;
    seen.clear();
    ctx.ui.setStatus("matrix-room-lease", undefined);
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = undefined;
    delete process.env.PI_ROOM_LEASE_INBOX;
    delete process.env.PI_ROOM_LEASE_SECRET;
    delete process.env.PI_ROOM_LEASE_BRIDGE_SOCKET;
    if (server) await new Promise<void>((resolve) => server!.close(() => resolve()));
    if (directory) rmSync(directory, { recursive: true, force: true });
    server = undefined;
    directory = undefined;
  });
}
