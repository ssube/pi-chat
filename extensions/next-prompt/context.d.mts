import type { SessionEntry } from "@earendil-works/pi-coding-agent";
export function recentConversation(entries: readonly SessionEntry[]): string;
export function suggestionPrompt(topic: string | undefined, conversation: string): string;
