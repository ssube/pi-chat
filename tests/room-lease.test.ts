import { test, expect } from "bun:test";
import { connect, createServer } from "node:net";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import roomLease, { formatMatrixMessage } from "../extensions/room-lease.ts";

async function deliver(id = "$reply", image?: { type: string; data: string; mimeType: string },
                       reply?: { event_id: string; sender: string; body: string },
                       ping?: boolean, edit?: { original: string; body: string }) {
  return await new Promise<string>((resolve, reject) => {
    const connection = connect(process.env.PI_ROOM_LEASE_INBOX!, () => {
      connection.write(JSON.stringify({
        secret: process.env.PI_ROOM_LEASE_SECRET,
        room: "!private:example.test",
        event: { type: "m.room.message", event_id: id,
                 sender: "@peer:example.test",
                 content: { msgtype: image ? "m.image" : "m.text",
                   body: edit ? `* ${edit.body}` : reply ? "> <@agent_one> earlier text\n\nYes, received." : "hi",
                   ...(reply && { "m.relates_to": { "m.in_reply_to": { event_id: reply.event_id } } }),
                   ...(edit && { "m.relates_to": { rel_type: "m.replace", event_id: edit.original },
                                 "m.new_content": { msgtype: "m.text", body: edit.body } }) } },
        image, reply, ping, recipient: "@agent_one:example.test",
      }) + "\n");
    });
    connection.once("data", data => resolve(data.toString()));
    connection.once("error", reject);
  });
}

test("interactive Pi receives a bridged Matrix reply in the same session", async () => {
  const handlers = new Map<string, Function>();
  const received: Array<[string, unknown]> = [];
  roomLease({
    on(name: string, handler: Function) { handlers.set(name, handler); },
    sendUserMessage(message: string, options: unknown) { received.push([message, options]); },
  } as any);
  const previous = process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET;
  process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET = "/tmp/bridge-test.sock";
  try {
    let hasKey = false;
    const ctx = { mode: "tui", isIdle: () => false,
      model: { provider: "test", id: "mock" },
      modelRegistry: { getApiKeyAndHeaders: async () => hasKey ? { ok: true, apiKey: "test-key" } : { ok: false, error: "no key" } },
      ui: { setStatus() {}, notify() {} } };
    await handlers.get("session_start")!({}, ctx);
    expect(process.env.PI_ROOM_LEASE_BRIDGE_SOCKET).toBe("/tmp/bridge-test.sock");
    expect(JSON.parse(await deliver("$rejected")).ok).toBe(false);
    expect(received).toHaveLength(0);
    hasKey = true;
    const reply = await deliver("$reply", undefined, undefined, true);
    expect(JSON.parse(reply).ok).toBe(true);
    expect(received[0]).toEqual([
      "[Matrix · room !private:example.test · sender @peer:example.test · event $reply]\n" +
      "Ping: @agent_one:example.test\n\nMessage:\nhi",
      { deliverAs: "steer" },
    ]);
    expect(JSON.parse(await deliver()).ok).toBe(true);
    expect(received).toHaveLength(1);
    const image = { type: "image", mimeType: "image/png", data: Buffer.from("pngbytes").toString("base64") };
    expect(JSON.parse(await deliver("$image", image)).ok).toBe(true);
    expect(received[1][0]).toEqual([
      { type: "text", text: "[Matrix · room !private:example.test · sender @peer:example.test · event $image]\n\nMessage:\n[Image] hi" },
      image,
    ]);
    const large = { ...image, data: Buffer.alloc(80_000, 1).toString("base64") };
    expect(JSON.parse(await deliver("$large", large)).ok).toBe(true);
    expect((received[2][0] as any[])[1].data).toBe(large.data);
    let unblock!: () => void;
    const gate = new Promise<void>(resolve => { unblock = resolve; });
    ctx.modelRegistry.getApiKeyAndHeaders = async () => { await gate; return { ok: true, apiKey: "test-key" }; };
    const duplicates = [deliver("$concurrent"), deliver("$concurrent")];
    await Bun.sleep(10);
    unblock();
    expect((await Promise.all(duplicates)).every(response => JSON.parse(response).ok === true)).toBe(true);
    expect(received).toHaveLength(4);
    expect((received[3][0] as string)).toContain("event $concurrent");
    ctx.modelRegistry.getApiKeyAndHeaders = async () => ({ ok: false, error: "no key" });
    expect(JSON.parse(await deliver("$retry")).ok).toBe(false);
    ctx.modelRegistry.getApiKeyAndHeaders = async () => ({ ok: true, apiKey: "test-key" });
    expect(JSON.parse(await deliver("$retry")).ok).toBe(true);
    expect(received).toHaveLength(5);
  } finally {
    await handlers.get("session_shutdown")!({}, { ui: { setStatus() {} } });
    if (previous === undefined) delete process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET;
    else process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET = previous;
  }
});

test("interactive sender claims attention before PUT, receives reply and image, then releases it", async () => {
  const directory = mkdtempSync(join(tmpdir(), "pi-handoff-flow-"));
  const bridgeSocket = join(directory, "bridge.sock");
  const room = "!private:example.test";
  const sender = "@agent_one-agent:matrix.example.invalid";
  const recipient = "@agent_two-agent:matrix.example.invalid";
  let claimed = false;
  let puts = 0;
  const bridge = createServer(connection => connection.on("data", chunk => {
    const request = JSON.parse(chunk.toString());
    if (request.op === "acquire") claimed = true;
    if (request.op === "release_all") claimed = false;
    connection.end(JSON.stringify({ ok: true, created: request.op === "acquire",
      leases: claimed ? [{ room, seconds: 180 }] : [] }) + "\n");
  }));
  await new Promise<void>(resolve => bridge.listen(bridgeSocket, resolve));
  const matrix = Bun.serve({ port: 0, hostname: "127.0.0.1", fetch: async (request) => {
    if (request.url.endsWith("/account/whoami")) return Response.json({ user_id: sender });
    if (request.url.endsWith("/joined_members"))
      return Response.json({ joined: { [sender]: {}, [recipient]: {} } });
    if (request.method === "PUT") {
      if (!claimed) return new Response("not claimed", { status: 409 });
      const content = await request.json() as { body: string; "m.mentions": { user_ids: string[] } };
      expect(content["m.mentions"].user_ids).toEqual([recipient]);
      expect(content.body.startsWith(recipient)).toBe(true);
      puts++;
      return Response.json({ event_id: `$sent${puts}` });
    }
    return new Response("unknown", { status: 404 });
  } });
  const handlers = new Map<string, Function>();
  const received: Array<string | unknown[]> = [];
  roomLease({ on(name: string, handler: Function) { handlers.set(name, handler); },
    sendUserMessage(message: string | unknown[]) { received.push(message); } } as any, 40);
  const previous = process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET;
  process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET = bridgeSocket;
  const ctx = { mode: "tui", isIdle: () => true, model: { provider: "test", id: "mock" },
    modelRegistry: { getApiKeyAndHeaders: async () => ({ ok: true, apiKey: "fake-key" }) },
    ui: { setStatus() {}, notify() {} } };
  try {
    await handlers.get("session_start")!({}, ctx);
    const messageFile = join(directory, "message.txt");
    writeFileSync(messageFile, "Please reply.");
    const envFile = join(directory, "sender.env");
    writeFileSync(envFile, JSON.stringify({ matrix: { homeserver: `http://127.0.0.1:${matrix.port}`, user_id: sender, token_env: "MATRIX_TOKEN", rooms: [room] } }), { mode: 0o600 });
    const send = async () => {
      const child = Bun.spawn(["python3", "tools/message_agent.py",
        "--room-id", room, "--to", recipient, "--message-file", messageFile,
        "--profile-file", envFile], {
        cwd: join(import.meta.dir, ".."),
        env: { ...process.env, MATRIX_TOKEN: "fake-token" }, stdout: "pipe", stderr: "pipe",
      });
      const status = await child.exited;
      if (status !== 0) throw new Error((await new Response(child.stderr).text()).split("\n").slice(-8).join("\n"));
      return await new Response(child.stdout).text();
    };
    expect(await send()).toContain("sent event=$sent1");
    expect(JSON.parse(await deliver("$peer-reply", undefined,
      { event_id: "$sent1", sender, body: "Please reply." })).ok).toBe(true);
    expect(received[0]).toContain("In reply to Matrix · room !private:example.test · sender " + sender);
    expect(received[0]).toContain("> Please reply.\n\nMessage:\nYes, received.");
    writeFileSync(messageFile, "Thanks for the reply.");
    expect(await send()).toContain("sent event=$sent2");
    const image = { type: "image", mimeType: "image/png", data: Buffer.from("image-bytes").toString("base64") };
    expect(JSON.parse(await deliver("$peer-image", image)).ok).toBe(true);
    expect((received[1] as Array<{ type: string; data?: string }>)[1].data).toBe(image.data);
    expect(JSON.parse(await deliver("$edit1", undefined, undefined, false,
      { original: "$peer-reply", body: "first revision" })).ok).toBe(true);
    await Bun.sleep(15);
    expect(JSON.parse(await deliver("$edit2", undefined, undefined, false,
      { original: "$peer-reply", body: "final revision" })).ok).toBe(true);
    await Bun.sleep(25);
    expect(received).toHaveLength(2);
    await Bun.sleep(40);
    expect(received).toHaveLength(3);
    expect(received[2]).toContain("Matrix edit · room !private:example.test");
    expect(received[2]).toContain("edit event $edit2 · original event $peer-reply");
    expect(received[2]).toContain("Edited message:\nfinal revision");
    expect(received[2]).not.toContain("first revision");
    expect(JSON.parse(await deliver("$edit3", undefined, undefined, false,
      { original: "$peer-reply", body: "discard on shutdown" })).ok).toBe(true);
    await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
    await Bun.sleep(60);
    expect(received).toHaveLength(3);
    expect(claimed).toBe(false);
    expect(puts).toBe(2);
  } finally {
    if (process.env.PI_ROOM_LEASE_INBOX) await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
    matrix.stop();
    await new Promise<void>(resolve => bridge.close(() => resolve()));
    rmSync(directory, { recursive: true, force: true });
    if (previous === undefined) delete process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET;
    else process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET = previous;
  }
});

test("Matrix reply quotes one attributed original and strips client fallback", () => {
  const room = "!private:example.test";
  const event = { event_id: "$reply", sender: "@agent_two:example.test",
    content: { msgtype: "m.text", body: "> <@agent_one> earlier message\n\nI can see it.",
      "m.relates_to": { "m.in_reply_to": { event_id: "$original" } } } };
  const result = formatMatrixMessage(room, event,
    { event_id: "$original", sender: "@agent_one:example.test", body: "Agent_two, reply if you can see this.\nThanks." });
  expect(result).toBe(
    "[Matrix · room !private:example.test · sender @agent_two:example.test · event $reply]\n\n" +
    "In reply to Matrix · room !private:example.test · sender @agent_one:example.test · event $original:\n" +
    "> Agent_two, reply if you can see this.\n> Thanks.\n\nMessage:\nI can see it.");
  expect(result).not.toContain("earlier message");
  expect(formatMatrixMessage(room, event, undefined, true, "@agent_one:example.test"))
    .toContain("Ping: @agent_one:example.test\n");
  expect(formatMatrixMessage(room, event, undefined, false, "@agent_one:example.test"))
    .not.toContain("Ping:");
  expect(formatMatrixMessage(room, event,
    { event_id: "$original", sender: "@agent_one:example.test", body: "original" }, false, "@agent_one:example.test"))
    .toContain("In reply to Matrix · room !private:example.test · sender @agent_one:example.test");
  expect(formatMatrixMessage(room, event,
    { event_id: "$original", sender: "@agent_one:example.test", body: "original" }, true, "@agent_one:example.test"))
    .toContain("Ping: @agent_one:example.test\n\nIn reply to Matrix");
  expect(formatMatrixMessage(room, event, { event_id: "$missing", sender: "@wrong:example.test", body: "forged parent" }))
    .toContain("event $original (original unavailable)");
  expect(formatMatrixMessage(room, event, { event_id: "$missing", sender: "@wrong:example.test", body: "forged parent" }))
    .not.toContain("forged parent");
  expect(formatMatrixMessage(room, { ...event, content: { ...event.content, "m.relates_to": undefined } },
    { event_id: "$original", sender: "@agent_one:example.test", body: "not a reply" }))
    .not.toContain("In reply to Matrix");
  expect(formatMatrixMessage(room, { ...event, content: { ...event.content,
    body: "> <@agent_one> earlier message\nMy reply" } },
    { event_id: "$original", sender: "@agent_one:example.test", body: "x".repeat(4000) }))
    .toContain(`> ${"x".repeat(1600)}\n\nMessage:\nMy reply`);
});

test("lease status shows active ownership and warns when the bridge loses it", async () => {
  const directory = mkdtempSync(join(tmpdir(), "pi-status-test-"));
  const socket = join(directory, "bridge.sock");
  let active = true;
  let invalid = false;
  let released = 0;
  const server = createServer(connection => {
    connection.on("data", data => {
      const request = JSON.parse(data.toString());
      if (request.op === "release_all") { released++; active = false; }
      connection.end(JSON.stringify({ ok: true,
        leases: active ? [{ room: "!private:example.test", seconds: invalid ? "wrong" : 150 }] : [] }) + "\n");
    });
  });
  await new Promise<void>(resolve => server.listen(socket, resolve));
  const handlers = new Map<string, Function>();
  const statuses: Array<string | undefined> = [];
  const notices: string[] = [];
  roomLease({ on(name: string, handler: Function) { handlers.set(name, handler); },
              sendUserMessage() {} } as any);
  const prior = process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET;
  const priorLabelRoom = process.env.PI_ROOM_LEASE_LABEL_ROOM;
  const priorLabelName = process.env.PI_ROOM_LEASE_LABEL_NAME;
  process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET = socket;
  process.env.PI_ROOM_LEASE_LABEL_ROOM = "!private:example.test";
  process.env.PI_ROOM_LEASE_LABEL_NAME = "Private test room";
  const ctx = { mode: "tui", isIdle: () => true,
    ui: { setStatus(_key: string, value: string | undefined) { statuses.push(value); },
          notify(value: string) { notices.push(value); } } };
  try {
    await handlers.get("session_start")!({}, ctx);
    await Bun.sleep(5150);
    expect(statuses.some(value => value ===
      "Matrix attention (1): Private test room (!private:example.test) · 150s left")).toBe(true);
    invalid = true;
    await Bun.sleep(5150);
    expect(notices).toHaveLength(1);
    expect(notices[0]).toContain("connection lost");
    expect(statuses.at(-1)).toBeUndefined();
  } finally {
    await handlers.get("session_shutdown")!({ reason: "new" }, ctx);
    expect(released).toBe(1);
    active = true;
    await handlers.get("session_start")!({ reason: "resume" }, ctx);
    await handlers.get("session_shutdown")!({ reason: "resume" }, ctx);
    expect(released).toBe(2);
    await new Promise<void>(resolve => server.close(() => resolve()));
    rmSync(directory, { recursive: true, force: true });
    if (prior === undefined) delete process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET;
    else process.env.AGENT_BRIDGE_ROOM_LEASE_SOCKET = prior;
    if (priorLabelRoom === undefined) delete process.env.PI_ROOM_LEASE_LABEL_ROOM;
    else process.env.PI_ROOM_LEASE_LABEL_ROOM = priorLabelRoom;
    if (priorLabelName === undefined) delete process.env.PI_ROOM_LEASE_LABEL_NAME;
    else process.env.PI_ROOM_LEASE_LABEL_NAME = priorLabelName;
  }
}, 15000);
