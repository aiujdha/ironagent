---
summary: "CLI reference for `ironagent browser` (profiles, tabs, actions, Chrome MCP, and CDP)"
read_when:
  - You use `ironagent browser` and want examples for common tasks
  - You want to control a browser running on another machine via a node host
  - You want to attach to your local signed-in Chrome via Chrome MCP
title: "browser"
---

# `ironagent browser`

Manage IronAgent’s browser control server and run browser actions (tabs, snapshots, screenshots, navigation, clicks, typing).

Related:

- Browser tool + API: [Browser tool](/tools/browser)

## Common flags

- `--url <gatewayWsUrl>`: Gateway WebSocket URL (defaults to config).
- `--token <token>`: Gateway token (if required).
- `--timeout <ms>`: request timeout (ms).
- `--browser-profile <name>`: choose a browser profile (default from config).
- `--json`: machine-readable output (where supported).

## Quick start (local)

```bash
ironagent browser profiles
ironagent browser --browser-profile ironagent start
ironagent browser --browser-profile ironagent open https://example.com
ironagent browser --browser-profile ironagent snapshot
```

## If the command is missing

If `ironagent browser` is an unknown command, check `plugins.allow` in
`~/.ironagent/ironagent.json`.

When `plugins.allow` is present, the bundled browser plugin must be listed
explicitly:

```json5
{
  plugins: {
    allow: ["telegram", "browser"],
  },
}
```

`browser.enabled=true` does not restore the CLI subcommand when the plugin
allowlist excludes `browser`.

Related: [Browser tool](/tools/browser#missing-browser-command-or-tool)

## Profiles

Profiles are named browser routing configs. In practice:

- `ironagent`: launches or attaches to a dedicated IronAgent-managed Chrome instance (isolated user data dir).
- `user`: controls your existing signed-in Chrome session via Chrome DevTools MCP.
- custom CDP profiles: point at a local or remote CDP endpoint.

```bash
ironagent browser profiles
ironagent browser create-profile --name work --color "#FF5A36"
ironagent browser create-profile --name chrome-live --driver existing-session
ironagent browser delete-profile --name work
```

Use a specific profile:

```bash
ironagent browser --browser-profile work tabs
```

## Tabs

```bash
ironagent browser tabs
ironagent browser open https://docs.ironagent.ai
ironagent browser focus <targetId>
ironagent browser close <targetId>
```

## Snapshot / screenshot / actions

Snapshot:

```bash
ironagent browser snapshot
```

Screenshot:

```bash
ironagent browser screenshot
```

Navigate/click/type (ref-based UI automation):

```bash
ironagent browser navigate https://example.com
ironagent browser click <ref>
ironagent browser type <ref> "hello"
```

## Existing Chrome via MCP

Use the built-in `user` profile, or create your own `existing-session` profile:

```bash
ironagent browser --browser-profile user tabs
ironagent browser create-profile --name chrome-live --driver existing-session
ironagent browser create-profile --name brave-live --driver existing-session --user-data-dir "~/Library/Application Support/BraveSoftware/Brave-Browser"
ironagent browser --browser-profile chrome-live tabs
```

This path is host-only. For Docker, headless servers, Browserless, or other remote setups, use a CDP profile instead.

## Remote browser control (node host proxy)

If the Gateway runs on a different machine than the browser, run a **node host** on the machine that has Chrome/Brave/Edge/Chromium. The Gateway will proxy browser actions to that node (no separate browser control server required).

Use `gateway.nodes.browser.mode` to control auto-routing and `gateway.nodes.browser.node` to pin a specific node if multiple are connected.

Security + remote setup: [Browser tool](/tools/browser), [Remote access](/gateway/remote), [Tailscale](/gateway/tailscale), [Security](/gateway/security)
