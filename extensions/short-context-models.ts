import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { getAgentDir, ModelRuntime, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { Provider } from "@earendil-works/pi-ai";

type Selection = { provider: string; id: string };

export function parseSelections(value: unknown): Selection[] {
  if (!Array.isArray(value)) throw new Error("Short-context config must be an array of { provider, id } entries");
  const seen = new Set<string>();
  return value.map(entry => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)
      || Object.keys(entry).some(key => key !== "provider" && key !== "id")
      || typeof entry.provider !== "string" || !entry.provider.trim()
      || typeof entry.id !== "string" || !entry.id.trim()) {
      throw new Error("Short-context entries require only nonempty provider and id strings");
    }
    const key = JSON.stringify([entry.provider, entry.id]);
    if (seen.has(key)) throw new Error("Duplicate short-context model selection");
    seen.add(key);
    return { provider: entry.provider, id: entry.id };
  });
}

export default async function (pi: ExtensionAPI) {
  const agentDir = getAgentDir();
  let text: string;
  try {
    text = await readFile(join(agentDir, "short-context-models.json"), "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }
  const selections = parseSelections(JSON.parse(text));
  if (!selections.length) return;
  const runtime = await ModelRuntime.create({
    modelsPath: join(agentDir, "models.json"),
    authPath: join(agentDir, "auth.json"),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });
  // Validate the whole configuration before registering any provider.
  const providers: Provider[] = [];
  for (const providerId of new Set(selections.map(model => model.provider))) {
    const provider = runtime.getProvider(providerId);
    if (!provider) throw new Error("Short-context provider not found in local Pi runtime");
    const wireIds = new Map<string, string>();
    const copies = selections.filter(model => model.provider === providerId).map(({ id }) => {
      const original = runtime.getModel(providerId, id);
      if (!original) throw new Error("Short-context model not found in local Pi runtime");
      const shortId = `${id}-short`;
      if (runtime.getModel(providerId, shortId)) throw new Error("Short-context alias already defined");
      if (!Number.isSafeInteger(original.contextWindow) || original.contextWindow < 2) {
        throw new Error("Short-context source must have a valid context window of at least two tokens");
      }
      wireIds.set(shortId, id);
      const contextWindow = Math.floor(original.contextWindow / 2);
      return {
        ...original,
        id: shortId,
        name: `${original.name} (Short Context)`,
        contextWindow,
        maxTokens: Math.min(original.maxTokens, contextWindow),
      };
    });
    const wirePayload = (payload: unknown, id: string): unknown => {
      const originalId = wireIds.get(id);
      return originalId && typeof payload === "object" && payload !== null && !Array.isArray(payload)
        ? { ...payload, model: originalId }
        : payload;
    };
    // Adapt the provider boundary, not session events: compaction and direct
    // registry calls bypass before_provider_request. Canonicalize the model
    // argument too, for providers that put the ID in the URL instead of JSON.
    providers.push({
      ...provider,
      getModels: () => [...provider.getModels(), ...copies],
      getAllModels: () => [...(provider.getAllModels?.() ?? provider.getModels()), ...copies],
      stream(model, context, options) {
        const originalId = wireIds.get(model.id);
        if (!originalId) return provider.stream(model, context, options);
        const adapted = { ...options } as NonNullable<typeof options>;
        adapted.onPayload = async (payload, target) => wirePayload(
          (await options?.onPayload?.(payload, target)) ?? payload, model.id);
        return provider.stream({ ...model, id: originalId }, context, adapted);
      },
      streamSimple(model, context, options) {
        const originalId = wireIds.get(model.id);
        if (!originalId) return provider.streamSimple(model, context, options);
        return provider.streamSimple({ ...model, id: originalId }, context, {
          ...options,
          onPayload: async (payload, target) => wirePayload(
            (await options?.onPayload?.(payload, target)) ?? payload, model.id),
        });
      },
    });
  }
  for (const provider of providers) pi.registerProvider(provider);
}
