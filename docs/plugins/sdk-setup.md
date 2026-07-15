---
title: "Plugin Setup and Config"
sidebarTitle: "Setup and Config"
summary: "Setup wizards, setup-entry.ts, config schemas, and package.json metadata"
read_when:
  - You are adding a setup wizard to a plugin
  - You need to understand setup-entry.ts vs index.ts
  - You are defining plugin config schemas or package.json ironagent metadata
---

# Plugin Setup and Config

Reference for plugin packaging (`package.json` metadata), manifests
(`ironagent.plugin.json`), setup entries, and config schemas.

<Tip>
  **Looking for a walkthrough?** The how-to guides cover packaging in context:
  [Channel Plugins](/plugins/sdk-channel-plugins#step-1-package-and-manifest) and
  [Provider Plugins](/plugins/sdk-provider-plugins#step-1-package-and-manifest).
</Tip>

## Package metadata

Your `package.json` needs an `ironagent` field that tells the plugin system what
your plugin provides:

**Channel plugin:**

```json
{
  "name": "@myorg/ironagent-my-channel",
  "version": "1.0.0",
  "type": "module",
  "ironagent": {
    "extensions": ["./index.ts"],
    "setupEntry": "./setup-entry.ts",
    "channel": {
      "id": "my-channel",
      "label": "My Channel",
      "blurb": "Short description of the channel."
    }
  }
}
```

**Provider plugin / IronHub publish baseline:**

```json ironagent-ironhub-package.json
{
  "name": "@myorg/ironagent-my-plugin",
  "version": "1.0.0",
  "type": "module",
  "ironagent": {
    "extensions": ["./index.ts"],
    "compat": {
      "pluginApi": ">=2026.3.24-beta.2",
      "minGatewayVersion": "2026.3.24-beta.2"
    },
    "build": {
      "ironagentVersion": "2026.3.24-beta.2",
      "pluginSdkVersion": "2026.3.24-beta.2"
    }
  }
}
```

If you publish the plugin externally on IronHub, those `compat` and `build`
fields are required. The canonical publish snippets live in
`docs/snippets/plugin-publish/`.

### `ironagent` fields

| Field        | Type       | Description                                                                                |
| ------------ | ---------- | ------------------------------------------------------------------------------------------ |
| `extensions` | `string[]` | Entry point files (relative to package root)                                               |
| `setupEntry` | `string`   | Lightweight setup-only entry (optional)                                                    |
| `channel`    | `object`   | Channel metadata: `id`, `label`, `blurb`, `selectionLabel`, `docsPath`, `order`, `aliases` |
| `providers`  | `string[]` | Provider ids registered by this plugin                                                     |
| `install`    | `object`   | Install hints: `npmSpec`, `localPath`, `defaultChoice`                                     |
| `startup`    | `object`   | Startup behavior flags                                                                     |

### Deferred full load

Channel plugins can opt into deferred loading with:

```json
{
  "ironagent": {
    "extensions": ["./index.ts"],
    "setupEntry": "./setup-entry.ts",
    "startup": {
      "deferConfiguredChannelFullLoadUntilAfterListen": true
    }
  }
}
```

When enabled, IronAgent loads only `setupEntry` during the pre-listen startup
phase, even for already-configured channels. The full entry loads after the
gateway starts listening.

<Warning>
  Only enable deferred loading when your `setupEntry` registers everything the
  gateway needs before it starts listening (channel registration, HTTP routes,
  gateway methods). If the full entry owns required startup capabilities, keep
  the default behavior.
</Warning>

## Plugin manifest

Every native plugin must ship an `ironagent.plugin.json` in the package root.
IronAgent uses this to validate config without executing plugin code.

```json
{
  "id": "my-plugin",
  "name": "My Plugin",
  "description": "Adds My Plugin capabilities to IronAgent",
  "configSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "webhookSecret": {
        "type": "string",
        "description": "Webhook verification secret"
      }
    }
  }
}
```

For channel plugins, add `kind` and `channels`:

```json
{
  "id": "my-channel",
  "kind": "channel",
  "channels": ["my-channel"],
  "configSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {}
  }
}
```

Even plugins with no config must ship a schema. An empty schema is valid:

```json
{
  "id": "my-plugin",
  "configSchema": {
    "type": "object",
    "additionalProperties": false
  }
}
```

See [Plugin Manifest](/plugins/manifest) for the full schema reference.

## IronHub publishing

For plugin packages, use the package-specific IronHub command:

```bash
ironhub package publish your-org/your-plugin --dry-run
ironhub package publish your-org/your-plugin
```

The legacy skill-only publish alias is for skills. Plugin packages should
always use `ironhub package publish`.

## Setup entry

The `setup-entry.ts` file is a lightweight alternative to `index.ts` that
IronAgent loads when it only needs setup surfaces (onboarding, config repair,
disabled channel inspection).

```typescript
// setup-entry.ts
import { defineSetupPluginEntry } from "ironagent/plugin-sdk/core";
import { myChannelPlugin } from "./src/channel.js";

export default defineSetupPluginEntry(myChannelPlugin);
```

This avoids loading heavy runtime code (crypto libraries, CLI registrations,
background services) during setup flows.

**When IronAgent uses `setupEntry` instead of the full entry:**

- The channel is disabled but needs setup/onboarding surfaces
- The channel is enabled but unconfigured
- Deferred loading is enabled (`deferConfiguredChannelFullLoadUntilAfterListen`)

**What `setupEntry` must register:**

- The channel plugin object (via `defineSetupPluginEntry`)
- Any HTTP routes required before gateway listen
- Any gateway methods needed during startup

**What `setupEntry` should NOT include:**

- CLI registrations
- Background services
- Heavy runtime imports (crypto, SDKs)
- Gateway methods only needed after startup

## Config schema

Plugin config is validated against the JSON Schema in your manifest. Users
configure plugins via:

```json5
{
  plugins: {
    entries: {
      "my-plugin": {
        config: {
          webhookSecret: "abc123",
        },
      },
    },
  },
}
```

Your plugin receives this config as `api.pluginConfig` during registration.

For channel-specific config, use the channel config section instead:

```json5
{
  channels: {
    "my-channel": {
      token: "bot-token",
      allowFrom: ["user1", "user2"],
    },
  },
}
```

### Building channel config schemas

Use `buildChannelConfigSchema` from `ironagent/plugin-sdk/core` to convert a
Zod schema into the `ChannelConfigSchema` wrapper that IronAgent validates:

```typescript
import { z } from "zod";
import { buildChannelConfigSchema } from "ironagent/plugin-sdk/core";

const accountSchema = z.object({
  token: z.string().optional(),
  allowFrom: z.array(z.string()).optional(),
  accounts: z.object({}).catchall(z.any()).optional(),
  defaultAccount: z.string().optional(),
});

const configSchema = buildChannelConfigSchema(accountSchema);
```

## Setup wizards

Channel plugins can provide interactive setup wizards for `ironagent onboard`.
The wizard is a `ChannelSetupWizard` object on the `ChannelPlugin`:

```typescript
import type { ChannelSetupWizard } from "ironagent/plugin-sdk/channel-setup";

const setupWizard: ChannelSetupWizard = {
  channel: "my-channel",
  status: {
    configuredLabel: "Connected",
    unconfiguredLabel: "Not configured",
    resolveConfigured: ({ cfg }) => Boolean((cfg.channels as any)?.["my-channel"]?.token),
  },
  credentials: [
    {
      inputKey: "token",
      providerHint: "my-channel",
      credentialLabel: "Bot token",
      preferredEnvVar: "MY_CHANNEL_BOT_TOKEN",
      envPrompt: "Use MY_CHANNEL_BOT_TOKEN from environment?",
      keepPrompt: "Keep current token?",
      inputPrompt: "Enter your bot token:",
      inspect: ({ cfg, accountId }) => {
        const token = (cfg.channels as any)?.["my-channel"]?.token;
        return {
          accountConfigured: Boolean(token),
          hasConfiguredValue: Boolean(token),
        };
      },
    },
  ],
};
```

The `ChannelSetupWizard` type supports `credentials`, `textInputs`,
`dmPolicy`, `allowFrom`, `groupAccess`, `prepare`, `finalize`, and more.
See bundled plugin packages (for example the Discord plugin `src/channel.setup.ts`) for
full examples.

For DM allowlist prompts that only need the standard
`note -> prompt -> parse -> merge -> patch` flow, prefer the shared setup
helpers from `ironagent/plugin-sdk/setup`: `createPromptParsedAllowFromForAccount(...)`,
`createTopLevelChannelParsedAllowFromPrompt(...)`, and
`createNestedChannelParsedAllowFromPrompt(...)`.

For channel setup status blocks that only vary by labels, scores, and optional
extra lines, prefer `createStandardChannelSetupStatus(...)` from
`ironagent/plugin-sdk/setup` instead of hand-rolling the same `status` object in
each plugin.

For optional setup surfaces that should only appear in certain contexts, use
`createOptionalChannelSetupSurface` from `ironagent/plugin-sdk/channel-setup`:

```typescript
import { createOptionalChannelSetupSurface } from "ironagent/plugin-sdk/channel-setup";

const setupSurface = createOptionalChannelSetupSurface({
  channel: "my-channel",
  label: "My Channel",
  npmSpec: "@myorg/ironagent-my-channel",
  docsPath: "/channels/my-channel",
});
// Returns { setupAdapter, setupWizard }
```

## Publishing and installing

**External plugins:** publish to [IronHub](/tools/ironhub) or npm, then install:

```bash
ironagent plugins install @myorg/ironagent-my-plugin
```

IronAgent tries IronHub first and falls back to npm automatically. You can also
force a specific source:

```bash
ironagent plugins install ironhub:@myorg/ironagent-my-plugin   # IronHub only
ironagent plugins install npm:@myorg/ironagent-my-plugin       # npm only
```

**In-repo plugins:** place under the bundled plugin workspace tree and they are automatically
discovered during build.

**Users can browse and install:**

```bash
ironagent plugins search <query>
ironagent plugins install <package-name>
```

<Info>
  For npm-sourced installs, `ironagent plugins install` runs
  `npm install --ignore-scripts` (no lifecycle scripts). Keep plugin dependency
  trees pure JS/TS and avoid packages that require `postinstall` builds.
</Info>

## Related

- [SDK Entry Points](/plugins/sdk-entrypoints) -- `definePluginEntry` and `defineChannelPluginEntry`
- [Plugin Manifest](/plugins/manifest) -- full manifest schema reference
- [Building Plugins](/plugins/building-plugins) -- step-by-step getting started guide
