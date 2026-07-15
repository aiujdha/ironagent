import type { ChannelId } from "../../channels/plugins/types.js";
import type { IronAgentConfig } from "../../config/config.js";
import { resolveChannelGroupRequireMention } from "../../config/group-policy.js";
import type { GroupKeyResolution, SessionEntry } from "../../config/sessions.js";
import { isInternalMessageChannel } from "../../utils/message-channel.js";
import { normalizeGroupActivation } from "../group-activation.js";
import type { TemplateContext } from "../templating.js";
import { extractExplicitGroupId } from "./group-id.js";

const WHATSAPP_GROUP_INTRO_HINT =
  "WhatsApp IDs: SenderId is the participant JID (group participant id).";

const CHANNEL_LABELS: Partial<Record<ChannelId, string>> = {
  bluebubbles: "BlueBubbles",
  discord: "Discord",
  imessage: "iMessage",
  line: "LINE",
  signal: "Signal",
  slack: "Slack",
  telegram: "Telegram",
  webchat: "WebChat",
  whatsapp: "WhatsApp",
};

// Phase 10.2: groups.runtime.ts was removed; channel-specific group helpers
// are no longer available in this fork. We keep the public API of groups.ts
// stable but route mention-policy questions through the generic config helper.

function resolveGroupId(raw: string | undefined | null): string | undefined {
  const trimmed = (raw ?? "").trim();
  return extractExplicitGroupId(trimmed) ?? (trimmed || undefined);
}

function resolveLooseChannelId(raw?: string | null): ChannelId | null {
  const normalized = raw?.trim().toLowerCase();
  if (!normalized) {
    return null;
  }
  return normalized as ChannelId;
}

async function resolveRuntimeChannelId(raw?: string | null): Promise<ChannelId | null> {
  return resolveLooseChannelId(raw);
}

export async function resolveGroupRequireMention(params: {
  cfg: IronAgentConfig;
  ctx: TemplateContext;
  groupResolution?: GroupKeyResolution;
}): Promise<boolean> {
  const { cfg, ctx, groupResolution } = params;
  const rawChannel = groupResolution?.channel ?? ctx.Provider?.trim();
  const channel = await resolveRuntimeChannelId(rawChannel);
  if (!channel) {
    return true;
  }
  const groupId = groupResolution?.id ?? resolveGroupId(ctx.From);
  // Phase 10.2: channel-plugin-specific mention policy hooks removed; fall back
  // to the generic config-resolved policy directly.
  return resolveChannelGroupRequireMention({
    cfg,
    channel,
    groupId,
    accountId: ctx.AccountId,
  });
}

export function defaultGroupActivation(requireMention: boolean): "always" | "mention" {
  return !requireMention ? "always" : "mention";
}

function resolveProviderLabel(rawProvider: string | undefined): string {
  const providerKey = rawProvider?.trim().toLowerCase() ?? "";
  if (!providerKey) {
    return "chat";
  }
  if (isInternalMessageChannel(providerKey)) {
    return "WebChat";
  }
  const providerId = resolveLooseChannelId(rawProvider?.trim());
  if (providerId) {
    return CHANNEL_LABELS[providerId] ?? providerId;
  }
  return `${providerKey.at(0)?.toUpperCase() ?? ""}${providerKey.slice(1)}`;
}

export function buildGroupChatContext(params: { sessionCtx: TemplateContext }): string {
  const subject = params.sessionCtx.GroupSubject?.trim();
  const members = params.sessionCtx.GroupMembers?.trim();
  const providerLabel = resolveProviderLabel(params.sessionCtx.Provider);

  const lines: string[] = [];
  if (subject) {
    lines.push(`You are in the ${providerLabel} group chat "${subject}".`);
  } else {
    lines.push(`You are in a ${providerLabel} group chat.`);
  }
  if (members) {
    lines.push(`Participants: ${members}.`);
  }
  lines.push(
    "Your replies are automatically sent to this group chat. Do not use the message tool to send to this same group — just reply normally.",
  );
  return lines.join(" ");
}

export function buildGroupIntro(params: {
  cfg: IronAgentConfig;
  sessionCtx: TemplateContext;
  sessionEntry?: SessionEntry;
  defaultActivation: "always" | "mention";
  silentToken: string;
}): string {
  const activation =
    normalizeGroupActivation(params.sessionEntry?.groupActivation) ?? params.defaultActivation;
  const providerId = resolveLooseChannelId(params.sessionCtx.Provider?.trim());
  const activationLine =
    activation === "always"
      ? "Activation: always-on (you receive every group message)."
      : "Activation: trigger-only (you are invoked only when explicitly mentioned; recent context may be included).";
  const providerIdsLine = providerId === "whatsapp" ? WHATSAPP_GROUP_INTRO_HINT : undefined;
  const silenceLine =
    activation === "always"
      ? `If no response is needed, reply with exactly "${params.silentToken}" (and nothing else) so IronAgent stays silent. Do not add any other words, punctuation, tags, markdown/code blocks, or explanations.`
      : undefined;
  const cautionLine =
    activation === "always"
      ? "Be extremely selective: reply only when directly addressed or clearly helpful. Otherwise stay silent."
      : undefined;
  const lurkLine =
    "Be a good group participant: mostly lurk and follow the conversation; reply only when directly addressed or you can add clear value. Emoji reactions are welcome when available.";
  const styleLine =
    "Write like a human. Avoid Markdown tables. Don't type literal \\n sequences; use real line breaks sparingly.";
  return [activationLine, providerIdsLine, silenceLine, cautionLine, lurkLine, styleLine]
    .filter(Boolean)
    .join(" ")
    .concat(" Address the specific sender noted in the message context.");
}
