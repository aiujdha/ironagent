# Go Gateway r11 镜像与新增配置更新手册

日期：2026-06-08

目标：上线本次 Go Gateway 并发与 callback/files 准确性修复，使 8 个任务可以同时进入 runtime in-flight，并在严格无 retry 条件下返回准确终态 callback 和 files[]。

## 更新范围

本次线上至少需要更新：

1. `go-gateway` Deployment 镜像。
2. `go-gateway-config` ConfigMap 中的新增配置。
3. 严格无 retry 相关配置确认。

不包含 MySQL / Redis 镜像更新。

## 业务镜像包

本次非 MySQL/Redis 的业务镜像 tar 一共 5 个：

```text
/home/ubuntu/image-export/go-gateway-callback-files-8concurrency-20260608-r11-errorloc-active-tags-amd64.tar
/home/ubuntu/image-export/ironagent-runtime-pytools-nodefix-20260604-r1-fc0e2e9a-amd64.tar
/home/ubuntu/image-export/ironagent-callback-receiver-callback-receiver-20260604-r2-fc0e2e9a-amd64.tar
/home/ubuntu/image-export/ironagent-admin-runtimeprobe-20260530-r2-local-aliases-amd64.tar
/home/ubuntu/image-export/ironagent-security-service-claim-lease-runtimeprobe-20260530-r2-local-aliases-amd64.tar
```

用途区分：

| tar | 用途 | 本次 Go Gateway r11 配置更新是否必需 |
|---|---|---|
| `go-gateway-callback-files-8concurrency-20260608-r11-errorloc-active-tags-amd64.tar` | Go Gateway 本次新版本 | 必需 |
| `ironagent-callback-receiver-callback-receiver-20260604-r2-fc0e2e9a-amd64.tar` | callback receiver 独立镜像 | 如果线上没有 callback receiver 或镜像不对，则必需 |
| `ironagent-runtime-pytools-nodefix-20260604-r1-fc0e2e9a-amd64.tar` | runtime / gateway / cli / assistant pods 镜像 | 不是本次 Go Gateway r11 最小更新必需；用于完整业务镜像备份/恢复 |
| `ironagent-admin-runtimeprobe-20260530-r2-local-aliases-amd64.tar` | admin 本地别名镜像 | 不是本次 Go Gateway r11 最小更新必需；用于完整业务镜像备份/恢复 |
| `ironagent-security-service-claim-lease-runtimeprobe-20260530-r2-local-aliases-amd64.tar` | security-service 本地别名镜像 | 不是本次 Go Gateway r11 最小更新必需；用于完整业务镜像备份/恢复 |

也就是说：镜像包是 5 个；但本次“更换镜像 + 添加 Go Gateway 配置”的最小上线动作，核心只要求更换 `go-gateway` 镜像。callback receiver 是否需要更新，取决于线上是否已经有可用的 `ironagent-callback-receiver` Deployment/Service 和正确镜像。

DB schema 修复需要额外使用这两个重新构建的迁移镜像。旧 admin/security 镜像里的 Alembic head 只有 `022_collaboration_repository_tables`，不能创建 `gateway_runtime_instances` 和 `gateway_runtime_leases`：

```text
/home/ubuntu/image-export/ironagent-admin-migrations-025-readyzfix-20260608-r2-amd64.tar
/home/ubuntu/image-export/ironagent-security-service-migrations-025-readyzfix-20260608-r2-amd64.tar
```

对应 tag：

```text
localhost/ironagent-admin:migrations-025-readyzfix-20260608-r2
localhost/ironagent-security-service:migrations-025-readyzfix-20260608-r2
```

这两个镜像均已验证包含：

```text
023_gateway_runtime_instances.py
024_gateway_conversation_routes.py
025_workspace_files_active_path_key.py
```

`alembic heads` 期望为：

```text
025_workspace_files_active_path_key (head)
```

使用 r2，不要使用 r1。r2 额外修复了 collaboration readyz 的 Alembic revision 判断：线上 `alembic_version` 升到 `025_workspace_files_active_path_key` 后，单 head 表里不会继续保留 `022_collaboration_repository_tables`；r2 会按 migration 祖先关系确认 `025` 已覆盖 `022`，因此不会再误报 `collaboration repository schema is not ready`。

## 目标 Go Gateway 镜像

Go Gateway 目标镜像：

```text
localhost/go-gateway:callback-files-8concurrency-20260608-r11-errorloc
```

本地已保存的 tar：

```text
/path/to/ironagent/output/image-export/go-gateway-callback-files-8concurrency-20260608-r11-errorloc-active-tags-amd64.tar
```

tar 内包含两个 tag，二者指向同一个镜像：

```text
localhost/go-gateway:callback-files-8concurrency-20260608-r11-errorloc
localhost/go-gateway:callback-files-8concurrency-20260608-r10-errorloc
```

tar sha256：

```text
c607a0fd5b74360fdcd90777b64a057c4ed17cd7f140057ee1bc96639f8a2e10
```

## 必须新增的 ConfigMap 配置

ConfigMap：`go-gateway-config`

新增配置：

```yaml
GO_GATEWAY_PROCESS_ROLE: all
GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED: "true"
GO_GATEWAY_TASK_WORKER_CONCURRENCY: "16"
GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT: 6m
```

严格无 retry 必须确认：

```yaml
GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS: "1"
```

推荐同时确认这些已有配置：

```yaml
GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT: "1"
GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT: 300s
GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT: 5m
```

说明：

- `GO_GATEWAY_TASK_WORKER_CONCURRENCY=16`：允许单个 Go Gateway 进程同时跑多个 task worker。
- `GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT=1`：每个 worker 每次只 claim 1 个任务，避免一个 worker 批量抢走任务。
- `GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS=1`：严格无 retry；失败或超时后不再重新 claim。
- `GO_GATEWAY_PROCESS_ROLE=all`：同一进程同时运行 API、task worker、callback worker。
- `GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED=true`：启用 callback outbox worker。

## 线上直接更新步骤

以下命令在有 kubeconfig 的控制端执行。命名空间默认为 `ironagent`。

```bash
export NS=ironagent
export K=kubectl
export GO_GATEWAY_IMAGE=localhost/go-gateway:callback-files-8concurrency-20260608-r11-errorloc
```

### 1. 备份当前线上状态

```bash
mkdir -p /tmp/go-gateway-r11-update-$(date +%Y%m%d%H%M%S)
export BACKUP_DIR=$(ls -td /tmp/go-gateway-r11-update-* | head -1)

$K -n "$NS" get deploy go-gateway -o yaml > "$BACKUP_DIR/go-gateway-deployment.before.yaml"
$K -n "$NS" get configmap go-gateway-config -o yaml > "$BACKUP_DIR/go-gateway-config.before.yaml"

echo "$BACKUP_DIR"
```

### 2. 确认镜像已在节点运行时中

如果镜像 tar 还没复制到服务器，先把本地 tar 复制到服务器推荐目录：

```bash
scp /path/to/ironagent/output/image-export/go-gateway-callback-files-8concurrency-20260608-r11-errorloc-active-tags-amd64.tar \
  <user>@<server>:/home/ubuntu/image-export/
```

在服务器节点上导入 containerd：

```bash
sudo ctr -n k8s.io images import /home/ubuntu/image-export/go-gateway-callback-files-8concurrency-20260608-r11-errorloc-active-tags-amd64.tar
sudo crictl images | grep 'localhost/go-gateway'
```

如果使用 `nerdctl`：

```bash
sudo nerdctl -n k8s.io load -i /home/ubuntu/image-export/go-gateway-callback-files-8concurrency-20260608-r11-errorloc-active-tags-amd64.tar
sudo nerdctl -n k8s.io images | grep 'localhost/go-gateway'
```

### 3. Patch `go-gateway-config`

```bash
$K -n "$NS" patch configmap go-gateway-config --type merge -p '{
  "data": {
    "GO_GATEWAY_PROCESS_ROLE": "all",
    "GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED": "true",
    "GO_GATEWAY_TASK_WORKER_CONCURRENCY": "16",
    "GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT": "6m",
    "GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS": "1",
    "GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT": "1",
    "GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT": "300s",
    "GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT": "5m"
  }
}'
```

### 4. 更新 Go Gateway 镜像

```bash
$K -n "$NS" set image deploy/go-gateway go-gateway="$GO_GATEWAY_IMAGE"
```

如果只改了 ConfigMap，Deployment 不会自动重启；这里更换镜像会触发 rollout。需要强制重启时执行：

```bash
$K -n "$NS" rollout restart deploy/go-gateway
```

### 5. 等待 rollout 完成

```bash
$K -n "$NS" rollout status deploy/go-gateway --timeout=300s
```

### 6. 用新 admin/security 镜像修复 DB schema

如果线上执行 `alembic current` 只显示：

```text
022_collaboration_repository_tables (head)
```

说明当前 admin/security 镜像不包含 023/024/025 migration。先导入新 admin 或 security-service migration 镜像：

```bash
sudo ctr -n k8s.io images import /home/ubuntu/image-export/ironagent-admin-migrations-025-readyzfix-20260608-r2-amd64.tar
sudo ctr -n k8s.io images import /home/ubuntu/image-export/ironagent-security-service-migrations-025-readyzfix-20260608-r2-amd64.tar
```

推荐先临时把 `deploy/admin` 切到新 admin 镜像，跑完 migration：

```bash
$K -n "$NS" set image deploy/admin admin=localhost/ironagent-admin:migrations-025-readyzfix-20260608-r2
$K -n "$NS" rollout status deploy/admin --timeout=300s

$K -n "$NS" exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini upgrade head &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini current
'
```

期望 current：

```text
025_workspace_files_active_path_key (head)
```

如果 `current` 已经是 `025`，但 MySQL 里仍然缺少 `gateway_runtime_instances` / `gateway_runtime_leases`，说明线上 `alembic_version` 被推进到了 `025`，但 023 的真实 DDL 没有落库。先确认：

```sql
SELECT version_num FROM alembic_version;
SHOW TABLES LIKE 'gateway_runtime%';
SHOW TABLES LIKE 'gateway_conversation_routes';
SHOW COLUMNS FROM workspace_files LIKE 'active_path_key';
```

异常形态：

```text
alembic_version = 025_workspace_files_active_path_key
SHOW TABLES LIKE 'gateway_runtime%' 只看到 gateway_runtimes / gateway_runtime_heartbeats
```

这种情况下，单纯再次执行 `alembic upgrade head` 不会补表，因为 Alembic 已经认为数据库在 head。需要用 r2 镜像把版本临时 stamp 回 `022`，再重新 upgrade 到 head。023/024/025 migration 都有已存在检查，可用于这个修复路径：

```bash
$K -n "$NS" exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini stamp 022_collaboration_repository_tables &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini upgrade head &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini current
'
```

修复后必须确认：

```sql
SHOW TABLES LIKE 'gateway_runtime%';
```

应至少包含：

```text
gateway_runtime_heartbeats
gateway_runtime_instances
gateway_runtime_leases
gateway_runtimes
```

然后重启 Go Gateway，让 runtime registry 重新工作：

```bash
$K -n "$NS" rollout restart deploy/go-gateway
$K -n "$NS" rollout status deploy/go-gateway --timeout=300s
```

## 更新仓库 YAML 的方式

如果走 GitOps / Kustomize，应更新这些文件。

### 1. 更新 ConfigMap patch

文件：

```text
deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/go-gateway-nodeport-config-patch.yaml
```

确保包含：

```yaml
data:
  GO_GATEWAY_PROCESS_ROLE: all
  GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED: "true"
  GO_GATEWAY_TASK_WORKER_CONCURRENCY: "16"
  GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT: 6m
  GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS: "1"
  GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT: "1"
  GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT: 300s
  GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT: 5m
```

### 2. 更新镜像 replacement

文件：

```text
deployment/kustomize/ironagent/overlays/runtime-k8s-pvc/kustomization.yaml
```

将 Go Gateway image replacement 调整为：

```yaml
images:
  - name: registry.example.com/ironagent/go-gateway
    newName: localhost/go-gateway
    newTag: callback-files-8concurrency-20260608-r11-errorloc
```

### 3. 渲染并应用

```bash
kubectl apply -k deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport
kubectl -n ironagent rollout status deploy/go-gateway --timeout=300s
```

## 验证

### 1. 验证 Deployment 镜像

```bash
$K -n "$NS" get deploy go-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="go-gateway")].image}{"\n"}'
```

期望输出：

```text
localhost/go-gateway:callback-files-8concurrency-20260608-r11-errorloc
```

### 2. 验证 ConfigMap

```bash
$K -n "$NS" get configmap go-gateway-config -o jsonpath='
PROCESS_ROLE={.data.GO_GATEWAY_PROCESS_ROLE}
CALLBACK_OUTBOX={.data.GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED}
WORKER_CONCURRENCY={.data.GO_GATEWAY_TASK_WORKER_CONCURRENCY}
DRAIN_TIMEOUT={.data.GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT}
MAX_ATTEMPTS={.data.GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS}
CLAIM_LIMIT={.data.GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT}
STALE_TIMEOUT={.data.GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT}
RUNTIME_TIMEOUT={.data.GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT}
'
```

期望：

```text
PROCESS_ROLE=all
CALLBACK_OUTBOX=true
WORKER_CONCURRENCY=16
DRAIN_TIMEOUT=6m
MAX_ATTEMPTS=1
CLAIM_LIMIT=1
STALE_TIMEOUT=300s
RUNTIME_TIMEOUT=5m
```

### 3. 验证 Pod 环境变量

```bash
$K -n "$NS" exec deploy/go-gateway -- sh -lc '
env | grep -E "GO_GATEWAY_PROCESS_ROLE|GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED|GO_GATEWAY_TASK_WORKER_CONCURRENCY|GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT|GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS|GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT|GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT|GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT" | sort
'
```

### 4. 验证 readyz

```bash
curl -fsS http://192.0.2.10:30808/readyz | jq .
```

至少应满足：

- `ready=true`
- `gateway=up`
- `task_queue=up`
- `southbound_admin=up`
- `southbound_security=up`

`container_scheduler=degraded` 需要结合 runtime ready/draining 数量判断；它不直接等价于本次 Go Gateway 配置失败。

## 回滚

### 1. 回滚 Deployment 镜像

查看历史：

```bash
$K -n "$NS" rollout history deploy/go-gateway
```

回滚到上一版：

```bash
$K -n "$NS" rollout undo deploy/go-gateway
$K -n "$NS" rollout status deploy/go-gateway --timeout=300s
```

或者手动设回旧镜像：

```bash
$K -n "$NS" set image deploy/go-gateway go-gateway=<previous-image>
$K -n "$NS" rollout status deploy/go-gateway --timeout=300s
```

### 2. 回滚 ConfigMap

使用备份恢复：

```bash
$K -n "$NS" apply -f "$BACKUP_DIR/go-gateway-config.before.yaml"
$K -n "$NS" rollout restart deploy/go-gateway
$K -n "$NS" rollout status deploy/go-gateway --timeout=300s
```

如果只想手动回退严格无 retry，可先把 attempts 恢复为旧值：

```bash
$K -n "$NS" patch configmap go-gateway-config --type merge -p '{
  "data": {
    "GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS": "2"
  }
}'
$K -n "$NS" rollout restart deploy/go-gateway
```

## 上线检查清单

- [ ] Go Gateway r11 tar 已导入节点运行时。
- [ ] `deploy/go-gateway` 镜像为 `localhost/go-gateway:callback-files-8concurrency-20260608-r11-errorloc`。
- [ ] `GO_GATEWAY_PROCESS_ROLE=all`。
- [ ] `GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED=true`。
- [ ] `GO_GATEWAY_TASK_WORKER_CONCURRENCY=16`。
- [ ] `GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT=6m`。
- [ ] `GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS=1`。
- [ ] `GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT=1`。
- [ ] `GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT=300s`。
- [ ] `GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT=5m`。
- [ ] `rollout status deploy/go-gateway` 成功。
- [ ] `/readyz` 返回 `ready=true`。
