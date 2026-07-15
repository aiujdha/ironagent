import { Type } from "@sinclair/typebox";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IronAgentConfig } from "../../config/config.js";
import { setActivePluginRegistry } from "../../plugins/runtime.js";
import { defaultRuntime } from "../../runtime.js";
import {
  createChannelTestPluginBase,
  createTestRegistry,
} from "../../test-utils/channel-plugins.js";
import {
  __testing,
  channelSupportsMessageCapability,
  channelSupportsMessageCapabilityForChannel,
  listChannelMessageActions,
  listChannelMessageCapabilities,
  listChannelMessageCapabilitiesForChannel,
  resolveChannelMessageToolSchemaProperties,
} from "./message-action-discovery.js";
import type { ChannelMessageCapability } from "./message-capabilities.js";
import type { ChannelPlugin } from "./types.js";

const emptyRegistry = createTestRegistry([]);

function createMessageActionsPlugin(params: {
  id: "demo-buttons" | "demo-cards";
  capabilities: readonly ChannelMessageCapability[];
  aliases?: string[];
}): ChannelPlugin {
  const base = createChannelTestPluginBase({
    id: params.id,
    label: params.id === "demo-buttons" ? "Demo Buttons" : "Demo Cards",
    capabilities: { chatTypes: ["direct", "group"] },
    config: {
      listAccountIds: () => ["default"],
    },
  });
  return {
    ...base,
    meta: {
      ...base.meta,
      ...(params.aliases ? { aliases: params.aliases } : {}),
    },
    actions: {
      describeMessageTool: () => ({
        actions: ["send"],
        capabilities: params.capabilities,
      }),
    },
  };
}

const buttonsPlugin = createMessageActionsPlugin({
  id: "demo-buttons",
  capabilities: ["interactive", "buttons"],
});

const cardsPlugin = createMessageActionsPlugin({
  id: "demo-cards",
  capabilities: ["cards"],
});

function activateMessageActionTestRegistry() {
  setActivePluginRegistry(
    createTestRegistry([
      { pluginId: "demo-buttons", source: "test", plugin: buttonsPlugin },
      { pluginId: "demo-cards", source: "test", plugin: cardsPlugin },
    ]),
  );
}

describe("message action capability checks", () => {
  const errorSpy = vi.spyOn(defaultRuntime, "error").mockImplementation(() => undefined);

  afterEach(() => {
    setActivePluginRegistry(emptyRegistry);
    __testing.resetLoggedMessageActionErrors();
    errorSpy.mockClear();
  });

  it("aggregates capabilities across plugins", () => {
    activateMessageActionTestRegistry();

    expect(listChannelMessageCapabilities({} as IronAgentConfig).toSorted()).toEqual([
      "buttons",
      "cards",
      "interactive",
    ]);
    expect(channelSupportsMessageCapability({} as IronAgentConfig, "interactive")).toBe(true);
    expect(channelSupportsMessageCapability({} as IronAgentConfig, "buttons")).toBe(true);
    expect(channelSupportsMessageCapability({} as IronAgentConfig, "cards")).toBe(true);
  });

  it("checks per-channel capabilities", () => {
    activateMessageActionTestRegistry();

    expect(
      listChannelMessageCapabilitiesForChannel({
        cfg: {} as IronAgentConfig,
        channel: "demo-buttons",
      }),
    ).toEqual(["interactive", "buttons"]);
    expect(
      listChannelMessageCapabilitiesForChannel({
        cfg: {} as IronAgentConfig,
        channel: "demo-cards",
      }),
    ).toEqual(["cards"]);
    expect(
      channelSupportsMessageCapabilityForChannel(
        { cfg: {} as IronAgentConfig, channel: "demo-buttons" },
        "interactive",
      ),
    ).toBe(true);
    expect(
      channelSupportsMessageCapabilityForChannel(
        { cfg: {} as IronAgentConfig, channel: "demo-cards" },
        "interactive",
      ),
    ).toBe(false);
    expect(
      channelSupportsMessageCapabilityForChannel(
        { cfg: {} as IronAgentConfig, channel: "demo-buttons" },
        "buttons",
      ),
    ).toBe(true);
    expect(
      channelSupportsMessageCapabilityForChannel(
        { cfg: {} as IronAgentConfig, channel: "demo-cards" },
        "buttons",
      ),
    ).toBe(false);
    expect(
      channelSupportsMessageCapabilityForChannel(
        { cfg: {} as IronAgentConfig, channel: "demo-cards" },
        "cards",
      ),
    ).toBe(true);
    expect(channelSupportsMessageCapabilityForChannel({ cfg: {} as IronAgentConfig }, "cards")).toBe(
      false,
    );
  });

  it("normalizes channel aliases for per-channel capability checks", () => {
    setActivePluginRegistry(
      createTestRegistry([
        {
          pluginId: "demo-cards",
          source: "test",
          plugin: createMessageActionsPlugin({
            id: "demo-cards",
            aliases: ["demo-cards-alias"],
            capabilities: ["cards"],
          }),
        },
      ]),
    );

    expect(
      listChannelMessageCapabilitiesForChannel({
        cfg: {} as IronAgentConfig,
        channel: "demo-cards-alias",
      }),
    ).toEqual(["cards"]);
  });

  it("uses unified message tool discovery for actions, capabilities, and schema", () => {
    const unifiedPlugin: ChannelPlugin = {
      ...createChannelTestPluginBase({
        id: "demo-unified",
        label: "Demo Unified",
        capabilities: { chatTypes: ["direct", "group"] },
        config: {
          listAccountIds: () => ["default"],
        },
      }),
      actions: {
        describeMessageTool: () => ({
          actions: ["react"],
          capabilities: ["interactive"],
          schema: {
            properties: {
              components: Type.Array(Type.String()),
            },
          },
        }),
      },
    };
    setActivePluginRegistry(
      createTestRegistry([{ pluginId: "demo-unified", source: "test", plugin: unifiedPlugin }]),
    );

    expect(listChannelMessageActions({} as IronAgentConfig)).toEqual(["send", "broadcast", "react"]);
    expect(listChannelMessageCapabilities({} as IronAgentConfig)).toEqual(["interactive"]);
    expect(
      resolveChannelMessageToolSchemaProperties({
        cfg: {} as IronAgentConfig,
        channel: "demo-unified",
      }),
    ).toHaveProperty("components");
  });

  it("skips crashing action/capability discovery paths and logs once", () => {
    const crashingPlugin: ChannelPlugin = {
      ...createChannelTestPluginBase({
        id: "demo-crashing",
        label: "Demo Crashing",
        capabilities: { chatTypes: ["direct", "group"] },
        config: {
          listAccountIds: () => ["default"],
        },
      }),
      actions: {
        describeMessageTool: () => {
          throw new Error("boom");
        },
      },
    };
    setActivePluginRegistry(
      createTestRegistry([{ pluginId: "demo-crashing", source: "test", plugin: crashingPlugin }]),
    );

    expect(listChannelMessageActions({} as IronAgentConfig)).toEqual(["send", "broadcast"]);
    expect(listChannelMessageCapabilities({} as IronAgentConfig)).toEqual([]);
    expect(errorSpy).toHaveBeenCalledTimes(1);

    expect(listChannelMessageActions({} as IronAgentConfig)).toEqual(["send", "broadcast"]);
    expect(listChannelMessageCapabilities({} as IronAgentConfig)).toEqual([]);
    expect(errorSpy).toHaveBeenCalledTimes(1);
  });
});
