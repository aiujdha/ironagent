# prod-k8s-single-entry-nodeport

This overlay is the first K8s-native runtime validation path.

It keeps the public surface on the single Go Gateway NodePort while moving
assistant runtime creation from host containerd to Kubernetes Pods:

- public entry: Go Gateway only
- runtime backend: `IRON_AGENT_RUNTIME_BACKEND=kubernetes`
- workspace storage: shared PVC `assistant-workspaces`
- Redis: in-cluster `redis-ha` StatefulSet with local PV-backed PVCs, plus official Redis Sentinel at `redis-sentinel:26379`
- assistant workspace: PVC subPath `assistants/<assistant_id>`
- runtime state: PVC subPath `assistants/<assistant_id>/.ironagent`
- per-assistant Service: not created by default
- Gateway runtime discovery: enabled so newly created running assistants are registered

This validation overlay binds `assistant-workspaces`, `mysql-data`, and the
`redis-data-redis-ha-{0,1}` PVCs to static local PVs on `vm-12-16-ubuntu`.
Edit `workspace-local-pv.yaml`, `mysql-local-pv.yaml`, and `redis-local-pv.yaml`
before applying this overlay to another cluster.

`GO_GATEWAY_RUNTIME_DISCOVER_ASSISTANTS=true` is set for this validation overlay
because the current Kubernetes runtime path does not include a runtime heartbeat
sidecar. Gateway periodically discovers running assistants from the control
plane so newly created assistants can accept `/api/v1/chat` tasks. Production
should still prefer a runtime heartbeat producer or sidecar that keeps every
active runtime registered through the Go Gateway runtime scope.

This overlay is for validation before replacing the existing containerd runtime
overlay. Use `prod-single-entry-nodeport` when you need the current live-compatible
containerd behavior.

Required checkout:

```bash
test -f deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/kustomization.yaml
```

If this returns non-zero, the current checkout does not contain this overlay.
Older `feat/go` checkouts may still be missing it. Sync the latest `feat/go`
branch before rendering:

```bash
git fetch origin feat/go
git checkout feat/go
git pull --ff-only origin feat/go
```

Render:

```bash
kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport \
  > /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml
```

Before applying, replace image tags and real secrets using the same rules as
`prod-single-entry-nodeport`. Redis and Sentinel read `REDIS_USERNAME` and
`REDIS_PASSWORD` from `control-plane-shared-secret`; Gateway uses
`GO_GATEWAY_REDIS_MODE=sentinel`,
`GO_GATEWAY_REDIS_SENTINEL_ADDRS=redis-sentinel:26379`, and
`GO_GATEWAY_REDIS_SENTINEL_MASTER=mymaster`. The compatibility `redis:6379`
service is not a master-aware write endpoint after failover.

Redis deployment check:

```bash
# Replace REDIS_PASSWORD in secret/control-plane.env locally before rendering.
# Do not commit real secret values. Preserve live Secrets before applying over
# an existing cluster so placeholder values do not overwrite production data.
kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport \
  > /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml

kubectl apply -f /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml
kubectl -n ironagent rollout status statefulset/redis-ha --timeout=180s
kubectl -n ironagent rollout status deployment/redis-sentinel --timeout=180s
kubectl -n ironagent get pod,pvc,pv -l app=redis-ha
kubectl -n ironagent get pod -l app=redis-sentinel

kubectl -n ironagent exec statefulset/redis-ha -- sh -ec '
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --user "$REDIS_USERNAME" del ironagent:redis:test
'

kubectl -n ironagent exec deploy/redis-sentinel -- sh -ec '
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -p 26379 sentinel get-master-addr-by-name mymaster
'
```
