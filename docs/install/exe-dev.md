---
summary: "Run IronAgent Gateway on exe.dev (VM + HTTPS proxy) for remote access"
read_when:
  - You want a cheap always-on Linux host for the Gateway
  - You want remote Control UI access without running your own VPS
title: "exe.dev"
---

# exe.dev

Goal: IronAgent Gateway running on an exe.dev VM, reachable from your laptop via: `https://<vm-name>.exe.xyz`

This page assumes exe.dev's default **exeuntu** image. If you picked a different distro, map packages accordingly.

## Beginner quick path

1. [https://exe.new/ironagent](https://exe.new/ironagent)
2. Fill in your auth key/token as needed
3. Click on "Agent" next to your VM and wait for Shelley to finish provisioning
4. Open `https://<vm-name>.exe.xyz/` and paste your gateway token to authenticate
5. Approve any pending device pairing requests with `ironagent devices approve <requestId>`

## What you need

- exe.dev account
- `ssh exe.dev` access to [exe.dev](https://exe.dev) virtual machines (optional)

## Automated Install with Shelley

Shelley, [exe.dev](https://exe.dev)'s agent, can install IronAgent instantly with our
prompt. The prompt used is as below:

```
Set up IronAgent (https://docs.ironagent.ai/install) on this VM. Use the non-interactive and accept-risk flags for ironagent onboarding. Add the supplied auth or token as needed. Configure nginx to forward from the default port 18789 to the root location on the default enabled site config, making sure to enable Websocket support. Pairing is done by "ironagent devices list" and "ironagent devices approve <request id>". Make sure the dashboard shows that IronAgent's health is OK. exe.dev handles forwarding from port 8000 to port 80/443 and HTTPS for us, so the final "reachable" should be <vm-name>.exe.xyz, without port specification.
```

## Manual installation

## 1) Create the VM

From your device:

```bash
ssh exe.dev new
```

Then connect:

```bash
ssh <vm-name>.exe.xyz
```

Tip: keep this VM **stateful**. IronAgent stores state under `~/.ironagent/` and `~/.ironagent/workspace/`.

## 2) Install prerequisites (on the VM)

```bash
sudo apt-get update
sudo apt-get install -y git curl jq ca-certificates openssl
```

## 3) Install IronAgent

Run the IronAgent install script:

```bash
curl -fsSL https://ironagent.ai/install.sh | bash
```

## 4) Setup nginx to proxy IronAgent to port 8000

Edit `/etc/nginx/sites-enabled/default` with

```
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    listen 8000;
    listen [::]:8000;

    server_name _;

    location / {
        proxy_pass http://127.0.0.1:18789;
        proxy_http_version 1.1;

        # WebSocket support
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Standard proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeout settings for long-lived connections
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
```

## 5) Access IronAgent and grant privileges

Access `https://<vm-name>.exe.xyz/` (see the Control UI output from onboarding). If it prompts for auth, paste the
token from `gateway.auth.token` on the VM (retrieve with `ironagent config get gateway.auth.token`, or generate one
with `ironagent doctor --generate-gateway-token`). Approve devices with `ironagent devices list` and
`ironagent devices approve <requestId>`. When in doubt, use Shelley from your browser!

## Remote Access

Remote access is handled by [exe.dev](https://exe.dev)'s authentication. By
default, HTTP traffic from port 8000 is forwarded to `https://<vm-name>.exe.xyz`
with email auth.

## Updating

```bash
npm i -g ironagent@latest
ironagent doctor
ironagent gateway restart
ironagent health
```

Guide: [Updating](/install/updating)
