# IronAgent Kubernetes Stack

This tree turns the current `ironagent` cluster into a repeatable Kustomize deployment instead of a one-off collection of hand-edited manifests.

## Overlays

- `base`: static workloads, services, template files, and the two live hotfixes the cluster is currently relying on.
- `overlays/runtime-common`: current live-compatible runtime shape.
  It keeps the workload pinned to `ironagent-worker`, retains the `hostAliases`, and preserves the privileged control-plane settings needed by the current containerd-based assistant runtime.
- `overlays/prod-nodeport`: matches the current public exposure model on `30789`, `30790`, `30800`, and `30880`.
- `overlays/prod-ingress`: recommended remote-access model.
  It keeps services internal and adds an HTTPS ingress for the browser-facing surfaces.

## Why this exists

The live cluster has drifted away from `deployment/*.yaml`:

- `admin` and `security-service` currently depend on a runtime `provisioner.py` hotfix.
- `security-service` currently depends on a patched `app.py`.
- the live Deployments also carry `hostPID`, `privileged`, `hostAliases`, and runtime-node pinning that were not fully expressed in the repo.

This Kustomize tree makes those requirements explicit so a future `kubectl apply` does not silently drop them.

## Current runtime assumptions

The stack is still running in a transitional architecture:

- all four core workloads run on one runtime worker (`ironagent-worker`)
- the control plane talks to `containerd` through `/run/containerd/containerd.sock`
- `admin` and `security-service` launch assistant runtimes through `/app/ironagent/ironagent-feat-hooks/bin/ironagent-docker`
- that wrapper depends on host `/usr/local/bin/nerdctl` plus `/usr/bin/nsenter`
- assistant runtimes publish through the configured host port range instead of ad hoc containerd namespace defaults
- runtime state lives on host paths under `/app/ironagent`

That means this stack is repeatable now, but it is not yet true multi-node high availability.

## First rollout

1. Fill in the placeholders under the overlay you want to use.
   The committed files intentionally contain `REPLACE_ME`.
2. Render and diff the manifests:

   ```bash
   ./scripts/k8s/ironagent-deploy.sh render prod-nodeport
   ./scripts/k8s/ironagent-deploy.sh diff prod-nodeport
   ```

3. Apply and wait for rollout:

   ```bash
   ./scripts/k8s/ironagent-deploy.sh apply prod-nodeport
   ```

4. Export the live state before any risky change:

   ```bash
   ./scripts/k8s/ironagent-export-live.sh tmp/ironagent-live-$(date +%Y%m%d-%H%M%S)
   ```

## When to use each overlay

- `prod-nodeport`: only for short-term compatibility with the current server exposure model.
  The browser Control UI over plain HTTP is still a weak fit because remote device identity wants a secure context.
- `prod-ingress`: the preferred target once an ingress controller and TLS certificate are ready.
  This is the path that aligns with the IronAgent Control UI security model.

## Hotfix provenance

The following files were vendored from the live cluster on `2026-04-20` so they are no longer implicit node-local dependencies:

- `base/files/hotfix/provisioner.py`
- `base/files/hotfix/security-service-app.py`
