// Keep built-in channel IDs in a leaf module so shared config/sandbox code can
// reference them without importing channel registry helpers that may pull in
// plugin runtime state.
// Phase 10.2: All bundled IM/SNS channel plugins were removed in this
// internal-only fork. The ordered list is kept as a typed `readonly string[]`
// so downstream callers continue to typecheck against `string`-shaped channel
// ids; at runtime the array is empty and no chat channels are bundled.
export const CHAT_CHANNEL_ORDER: readonly string[] = [];

export type ChatChannelId = string;

export const CHANNEL_IDS: readonly string[] = [...CHAT_CHANNEL_ORDER];
