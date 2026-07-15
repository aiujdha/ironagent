import { definePluginEntry } from "ironagent/plugin-sdk/plugin-entry";
import {
  handleAfterToolCall,
  handleBeforePromptBuild,
  handleBeforeToolCall,
  resolveIronAgentBridgeConfig,
} from "./api.js";

const PLUGIN_ID = "ironagent-bridge";

export default definePluginEntry({
  id: PLUGIN_ID,
  name: "IronAgent Bridge",
  description: "Bridges tool execution hooks to an external IronAgent security service",
  register(api) {
    api.on("before_prompt_build", async (event, ctx) => {
      const config = resolveIronAgentBridgeConfig();
      return await handleBeforePromptBuild(event, ctx, {
        config,
        logger: api.logger,
      });
    });

    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const config = resolveIronAgentBridgeConfig();
        return await handleBeforeToolCall(event, ctx, {
          config,
          logger: api.logger,
        });
      },
      { priority: 100 },
    );

    api.on("after_tool_call", async (event, ctx) => {
      const config = resolveIronAgentBridgeConfig();
      await handleAfterToolCall(event, ctx, {
        config,
        logger: api.logger,
      });
    });
  },
});
