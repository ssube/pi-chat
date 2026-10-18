// Model-switch contract: isolated synthetic providers, no network or owner auth.
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export default function (pi) {
    for (const provider of ["bridge-fast-offline", "bridge-smart-offline"]) {
        pi.registerProvider(provider, {
            baseUrl: "https://unused.invalid", apiKey: "offline-fixture", api: "bridge-modes-offline",
            models: [{ id: "test", name: provider, reasoning: false, input: ["text"],
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 8192, maxTokens: 512 }],
            streamSimple(model, context) {
                const stream = createAssistantMessageEventStream();
                const requests = context.messages.filter(message => message.role === "user").length;
                const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
                    content: [{ type: "text", text: `${model.provider}: ${requests} requests` }],
                    usage: { input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20,
                        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
                    stopReason: "stop", timestamp: Date.now() };
                queueMicrotask(() => {
                    stream.push({ type: "start", partial: message });
                    stream.push({ type: "done", reason: "stop", message });
                    stream.end();
                });
                return stream;
            },
        });
    }
}
