import type { ChannelId } from "../channels/plugins/types.js";
import type { PluginRuntime } from "../plugins/runtime/types.js";
import { normalizeAccountId } from "../routing/session-key.js";

// Phase 10.2: channel-runtime pairing API was removed alongside the channel
// plugins. We keep this scoped pairing helper as a typed stub so legacy
// production call sites still compile in the internal-only fork; at runtime
// nothing reaches these paths because no channel plugins are bundled.
type StubPairingChallengeOutcome = {
  code: string;
  created: boolean;
};

export function createScopedPairingAccess(params: {
  core: PluginRuntime;
  channel: ChannelId;
  accountId: string;
}) {
  const resolvedAccountId = normalizeAccountId(params.accountId);
  void params.core;
  return {
    accountId: resolvedAccountId,
    readAllowFromStore: (): unknown[] => [],
    readStoreForDmPolicy: (_provider: ChannelId, _accountId: string): unknown[] => [],
    upsertPairingRequest: async (_input: {
      id: string;
      meta?: Record<string, string | undefined>;
    }): Promise<StubPairingChallengeOutcome> => ({ code: "", created: false }),
  };
}
