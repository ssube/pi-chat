// Offline Pi contract fixture: no network, owner configuration, or credentials.
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export default function (pi) {
    let calls = 0;
    pi.registerTool({
        name: "view_test_image", label: "View synthetic image", description: "Return a synthetic PNG",
        parameters: { type: "object", properties: {}, additionalProperties: false },
        async execute() {
            return { content: [{ type: "image", mimeType: "image/png",
                data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7x8AAAAASUVORK5CYII=" }], details: {} };
        },
    });
    pi.on("session_before_compact", async (event) => ({ compaction: {
        summary: "Offline checkpoint retaining the image result",
        firstKeptEntryId: event.preparation.firstKeptEntryId,
        tokensBefore: event.preparation.tokensBefore,
    } }));
    pi.registerProvider("bridge-offline", {
        baseUrl: "https://unused.invalid", apiKey: "offline-fixture", api: "bridge-offline-api",
        models: [{ id: "test", name: "Offline bridge test", reasoning: false, input: ["text", "image"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 4096, maxTokens: 512 }],
        streamSimple(model) {
            const stream = createAssistantMessageEventStream();
            const tool = calls++ === 0;
            const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
                content: tool ? [{ type: "toolCall", id: "image-1", name: "view_test_image", arguments: {} }]
                              : [{ type: "text", text: "Image inspected successfully." }],
                usage: { input: 3500, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 3510,
                    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
                stopReason: tool ? "toolUse" : "stop", timestamp: Date.now() };
            queueMicrotask(() => {
                stream.push({ type: "start", partial: message });
                stream.push({ type: "done", reason: message.stopReason, message });
                stream.end();
            });
            return stream;
        },
    });
}
