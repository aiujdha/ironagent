import type { IronAgentConfig } from "../../config/config.js";
import type { WizardPrompter } from "../../wizard/prompts.js";
import { promptChannelAccessConfig, type ChannelAccessPolicy } from "./setup-group-access.js";

export async function configureChannelAccessWithAllowlist<TResolved>(params: {
  cfg: IronAgentConfig;
  prompter: WizardPrompter;
  label: string;
  currentPolicy: ChannelAccessPolicy;
  currentEntries: string[];
  placeholder: string;
  updatePrompt: boolean;
  skipAllowlistEntries?: boolean;
  setPolicy: (cfg: IronAgentConfig, policy: ChannelAccessPolicy) => IronAgentConfig;
  resolveAllowlist?: (params: { cfg: IronAgentConfig; entries: string[] }) => Promise<TResolved>;
  applyAllowlist?: (params: { cfg: IronAgentConfig; resolved: TResolved }) => IronAgentConfig;
}): Promise<IronAgentConfig> {
  let next = params.cfg;
  const accessConfig = await promptChannelAccessConfig({
    prompter: params.prompter,
    label: params.label,
    currentPolicy: params.currentPolicy,
    currentEntries: params.currentEntries,
    placeholder: params.placeholder,
    updatePrompt: params.updatePrompt,
    skipAllowlistEntries: params.skipAllowlistEntries,
  });
  if (!accessConfig) {
    return next;
  }
  if (accessConfig.policy !== "allowlist") {
    return params.setPolicy(next, accessConfig.policy);
  }
  if (params.skipAllowlistEntries || !params.resolveAllowlist || !params.applyAllowlist) {
    return params.setPolicy(next, "allowlist");
  }
  const resolved = await params.resolveAllowlist({
    cfg: next,
    entries: accessConfig.entries,
  });
  next = params.setPolicy(next, "allowlist");
  return params.applyAllowlist({
    cfg: next,
    resolved,
  });
}
