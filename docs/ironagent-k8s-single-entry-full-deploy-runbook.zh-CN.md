# IronAgent K8s 单入口全量部署手册

> 本手册由 2026-05-07 版演进而来，作为现行 runbook 持续维护；镜像 tag 与配置以 `deployment/kustomize` 当前内容为准。

本文档用于在一套新的 K8s 集群中复现当前线上形态：Go Gateway 单入口、Admin/Security/IronAgent/Callback Receiver/MySQL/Redis 内部服务、每个助手一个 K8s Pod、共享 PVC + subPath 工作空间。

## 1. 部署结论

| 项目 | 标准 |
|---|---|
| 对外入口 | 只暴露 Go Gateway |
| 默认公网端口 | `30808` |
| Namespace | `ironagent` |
| Runtime 后端 | `kubernetes` |
| 助手运行形态 | 每个助手一个 K8s Pod |
| 助手工作空间 | `assistant-workspaces` PVC 下的 `assistants/<assistant_id>` subPath |
| 助手容器内工作目录 | `/workspace` |
| 数据库 | K8s 内部 MySQL StatefulSet |
| 任务队列/缓存 | K8s 内部 Redis HA（`redis-ha` StatefulSet 主从 + `redis-sentinel` Deployment 三副本） |
| 文件后端 | 默认本地后端；后续可切 NAS/OBS/COS |
| WebUI | 通过 Go Gateway `/assistants/{assistant_id}/ui/` 代理访问 |

当前部署是单入口测试/准生产形态。单节点 local PV 可用于无 NAS/OBS 的环境；多节点生产应改成 RWX 存储，例如 NAS、NFS、CephFS。

## 2. 当前验证版本

业务镜像 tag 的单一事实来源是 `deployment/kustomize/ironagent/overlays/runtime-k8s-pvc/kustomization.yaml` 的 `images` 段，部署前以渲染结果为准确认：

```bash
kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport | grep "image: "
```

| 组件 | 镜像（`<tag>` 见上述 `images` 段） |
|---|---|
| Admin | `localhost/ironagent-admin:<tag>` |
| Security Service | `localhost/ironagent-security-service:<tag>` |
| Go Gateway | `localhost/go-gateway:<tag>` |
| IronAgent WebUI / Runtime | `localhost/ironagent:<tag>` |
| Callback Receiver | `localhost/ironagent-callback-receiver:<tag>` |
| MySQL | `mysql:8.4` |
| Redis | `redis:7.2.10-alpine` |

镜像 tar 包建议保留在构建机和目标 worker：

| 位置 | 用途 |
|---|---|
| `<构建机>:/root/image-export/<release>/` | 构建机归档 |
| `<worker>:/home/ubuntu/test/` | 目标节点导入 containerd |

## 3. 部署用到的 YAML

主入口：

```bash
deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport
```

关键文件：

| 文件 | 作用 |
|---|---|
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/kustomization.yaml` | 单入口部署入口 |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/service-nodeports-go-gateway.yaml` | 只暴露 Go Gateway `30808` |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/control-plane-k8s-runtime-config-patch.yaml` | 开启 K8s runtime、workspace PVC、runtime namespace |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/go-gateway-nodeport-config-patch.yaml` | Go Gateway 公网地址和内部服务地址 |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/mysql.yaml` | MySQL Service + StatefulSet |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/mysql-local-pv.yaml` | MySQL 本地 PV/PVC |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/redis.yaml` | Redis HA（`redis-ha` StatefulSet + `redis-sentinel` Deployment + Service） |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/redis-local-pv.yaml` | Redis 本地 PV（两个副本各一块） |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/workspace-local-pv.yaml` | workspace 本地 PV |
| `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/workspace-pvc-local-patch.yaml` | workspace PVC 绑定本地 StorageClass |
| `deployment/kustomize/ironagent/overlays/runtime-k8s-pvc/kustomization.yaml` | 镜像 tag、runtime RBAC、workspace PVC |
| `deployment/kustomize/ironagent/base/*.yaml` | 基础 Deployment/Service |

渲染前先确认目录存在：

```bash
cd <repo-root>
git fetch origin main
git checkout main
git pull --ff-only origin main

test -f deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/kustomization.yaml
kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport \
  > /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml
```

如果报 `not a valid directory` 或 `evalsymlink failure`，说明代码版本不包含该 overlay。不要继续部署，先同步最新 `main`。

## 4. 其他服务器必须适配的参数

| 参数 | 当前示例 | 迁移到其他服务器时如何确定 |
|---|---|---|
| `PUBLIC_IP` | `192.0.2.10` | 实际开放 `30808` 的公网 IP 或域名 |
| `NS` | `ironagent` | 可沿用；修改后所有命令同步改 namespace |
| `GO_GATEWAY_NODEPORT` | `30808` | 可沿用；如冲突需同时改 Service 和安全组 |
| `WORKER_HOSTNAME` | `vm-12-16-ubuntu` | `kubectl get nodes -o wide` 查看实际节点名 |
| workspace hostPath | `/app/ironagent/assistant-workspaces` | 目标 worker 上可持久化的大磁盘目录 |
| MySQL hostPath | `/app/ironagent/k8s-pv/mysql-data` | 目标 worker 上可持久化的大磁盘目录 |
| Redis hostPath | `/app/ironagent/k8s-pv/redis-data-0`、`/app/ironagent/k8s-pv/redis-data-1` | 目标 worker 上可持久化的目录，两个 Redis 副本各一块 |
| kubeconfig | `/etc/kubernetes/admin.conf` | 目标集群实际 kubeconfig |
| 镜像 tag | 第 2 节 | 必须与导入节点 containerd 的镜像完全一致 |
| Secret | 每套环境重新生成 | 不要复用测试环境 token |

建议先设置变量：

```bash
export NS=ironagent
export PUBLIC_IP=<target-public-ip>
export GO_GATEWAY_NODEPORT=30808
export WORKER_HOSTNAME=<target-worker-hostname>
export WORKSPACE_HOST_PATH=/app/ironagent/assistant-workspaces
export MYSQL_HOST_PATH=/app/ironagent/k8s-pv/mysql-data
export KUBECONFIG=/etc/kubernetes/admin.conf
```

## 5. ConfigMap 必改项

### 5.1 `control-plane-shared-config`

来源：

```text
deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/config/control-plane-shared.env
deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/control-plane-k8s-runtime-config-patch.yaml
```

必须根据目标环境修改：

| 配置 | 示例 | 说明 |
|---|---|---|
| `IRON_AGENT_CONTROL_UI_PUBLIC_URL` | `http://<PUBLIC_IP>:30808` | 后台打开助手 UI 使用 |
| `IRON_AGENT_GO_GATEWAY_PUBLIC_URL` | `http://<PUBLIC_IP>:30808` | Go Gateway 对外地址 |
| `IRON_AGENT_CONTROL_UI_ALLOWED_ORIGIN_REGEX` | `^http://<PUBLIC_IP>:30808$` | WebUI origin 允许列表 |
| `IRON_AGENT_RUNTIME_NO_PROXY` | `localhost,127.0.0.1,<PUBLIC_IP>,security-service,admin,.svc,.cluster.local` | 避免内部调用走代理 |
| `IRON_AGENT_RUNTIME_BACKEND` | `kubernetes` | 必须为 K8s runtime |
| `IRON_AGENT_K8S_RUNTIME_NAMESPACE` | `ironagent` | 助手 Pod 所在 namespace |
| `IRON_AGENT_K8S_RUNTIME_WORKSPACE_PVC` | `assistant-workspaces` | 助手共享 PVC |
| `IRON_AGENT_K8S_RUNTIME_WORKSPACE_SUBPATH_PREFIX` | `assistants` | 助手子目录前缀 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_BACKEND` | `local` | 当前本地文件后端 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_ROOT` | `/workspace-root/workspace-objects` | 本地对象文件根目录 |
| `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND` | `mysql` | workspace file/event/sync-job 元数据默认写 MySQL；显式设 `sqlite` 才回退共享 SQLite |
| `IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH` | `/workspace-root/control-plane/data/workspace_files/workspace-files.sqlite` | 仅 `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite` 时使用的 fallback 元数据 SQLite |
| `IRON_AGENT_WORKSPACE_PUSH_REQUESTS_STORE_PATH` | `/workspace-root/control-plane/data/workspace_files/push_requests.json` | 仅 sqlite/JSON fallback 路径使用；默认 MySQL 写 `external_push_requests` 表 |

注意：默认 MySQL 元数据路径依赖数据库表完成迁移。只有显式回退到 `sqlite` 时，`IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH` 和 `IRON_AGENT_WORKSPACE_PUSH_REQUESTS_STORE_PATH` 才必须放在 `admin` 与 `security-service` 都可见的共享 PVC 上；不要放在 `/app/...` 这类容器本地路径。

### 5.2 `go-gateway-config`

来源：

```text
deployment/kustomize/ironagent/base/go-gateway-configmap.yaml
deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/go-gateway-nodeport-config-patch.yaml
```

必须根据目标环境修改：

| 配置 | 示例 | 说明 |
|---|---|---|
| `GO_GATEWAY_PUBLIC_BASE_URL` | `http://<PUBLIC_IP>:30808` | fallback URL、对外链接生成 |
| `GO_GATEWAY_SECURITY_BASE_URL` | `http://security-service:8000` | 集群内 Service，通常不改 |
| `GO_GATEWAY_ADMIN_BASE_URL` | `http://admin:8501` | 集群内 Service，通常不改 |
| `GO_GATEWAY_CALLBACK_ALLOWED_HOSTS` | `ironagent-callback-receiver:8099,ironagent-callback-receiver` | 默认白名单为集群内 callback-receiver；接入外部上游时改为上游域名白名单 |
| `GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS` | `true` | 当前默认 callback 发往集群内 receiver，故为 `true`；callback 指向公网上游时应收紧为 `false` |
| `GO_GATEWAY_RUNTIME_DISCOVER_ASSISTANTS` | `true` | 当前验证 overlay 用于发现运行中助手 |
| `GO_GATEWAY_RUNTIME_SYNC_INTERVAL` | `30s` | runtime 同步周期 |
| `GO_GATEWAY_REDIS_MODE` | `sentinel` | 当前 overlay 使用 Redis Sentinel 形态 |
| `GO_GATEWAY_REDIS_SENTINEL_ADDRS` | `redis-sentinel:26379` | 集群内 Sentinel Service，通常不改 |
| `GO_GATEWAY_REDIS_SENTINEL_MASTER` | `mymaster` | Sentinel master 名称，与 `redis.yaml` 保持一致 |

### 5.3 `ironagent-gateway-config`

来源：

```text
deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/config/gateway.env
```

重点检查：

| 配置 | 说明 |
|---|---|
| `IRON_AGENT_GATEWAY_ALLOWED_ORIGINS` | 必须包含 `http://<PUBLIC_IP>:30808` |

设备身份校验目前由 runtime provisioner 在生成助手配置时默认关闭（写入 `dangerouslyDisableDeviceAuth: true`，见 `src/runtime/provisioner.py`），不再有 `IRON_AGENT_CONTROL_UI_ALLOW_INSECURE_DEVICE_IDENTITY` 开关；生产仍应使用 HTTPS。

## 6. Secret 来源和生成方式

仓库中的 `secret/*.env` 只能是占位值。真实值来自部署时生成，不应提交。

生成示例：

```bash
export IRON_AGENT_INTERNAL_SERVICE_TOKEN=$(openssl rand -hex 32)
export IRON_AGENT_GATEWAY_TOKEN=$(openssl rand -hex 32)
export IRON_AGENT_RUNTIME_HOOK_API_TOKEN=$(openssl rand -hex 32)

export MYSQL_ROOT_PASSWORD=$(openssl rand -hex 24)
export MYSQL_DATABASE=heinu
export MYSQL_USER=heinu
export MYSQL_PASSWORD=$(openssl rand -hex 24)

export REDIS_USERNAME=ironagent
export REDIS_PASSWORD=$(openssl rand -hex 24)

export IRON_AGENT_DATABASE_URL="mysql+pymysql://${MYSQL_USER}:${MYSQL_PASSWORD}@mysql:3306/${MYSQL_DATABASE}?charset=utf8mb4"

export GO_GATEWAY_PUBLIC_API_KEY=$(openssl rand -hex 24)
export GO_GATEWAY_RUNTIME_API_KEY=$(openssl rand -hex 24)
export GO_GATEWAY_DEBUG_API_KEY=$(openssl rand -hex 24)
export GO_GATEWAY_ADMIN_API_KEY=$(openssl rand -hex 24)
export GO_GATEWAY_PROXY_API_KEY=$(openssl rand -hex 24)
export GO_GATEWAY_SCOPED_API_KEYS="${GO_GATEWAY_PUBLIC_API_KEY}:public;${GO_GATEWAY_RUNTIME_API_KEY}:runtime;${GO_GATEWAY_DEBUG_API_KEY}:debug;${GO_GATEWAY_ADMIN_API_KEY}:admin;${GO_GATEWAY_PROXY_API_KEY}:proxy"

export GO_GATEWAY_HMAC_SECRET=$(openssl rand -hex 32)
export GO_GATEWAY_CALLBACK_SECRET=$(openssl rand -hex 32)
export GO_GATEWAY_PRESIGN_SECRET=$(openssl rand -hex 32)
export GO_GATEWAY_PROXY_SESSION_SECRET=$(openssl rand -hex 32)
```

把这些值写入临时 overlay 副本的 `secret/*.env`，渲染 apply 后删除临时副本。不要把真实 Secret 写回 Git。

必填 Secret：

| Secret | 字段 |
|---|---|
| `control-plane-shared-secret` | `IRON_AGENT_INTERNAL_SERVICE_TOKEN`、`IRON_AGENT_RUNTIME_HOOK_API_TOKEN`、`MYSQL_ROOT_PASSWORD`、`MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD`、`REDIS_USERNAME`、`REDIS_PASSWORD`、`IRON_AGENT_DATABASE_URL`（`IRON_AGENT_COS_*` 三项不用 COS 时留空） |
| `ironagent-gateway-secret` | `IRON_AGENT_GATEWAY_TOKEN` |
| `go-gateway-secret` | `GO_GATEWAY_DATABASE_URL`、`GO_GATEWAY_SCOPED_API_KEYS`、`GO_GATEWAY_HMAC_SECRET`、`GO_GATEWAY_CALLBACK_SECRET`、`GO_GATEWAY_PRESIGN_SECRET`、`GO_GATEWAY_PROXY_SESSION_SECRET` |

Go Gateway 调用内部服务的 token 不在 `go-gateway-secret` 中：deployment 直接从 `control-plane-shared-secret` 的 `IRON_AGENT_INTERNAL_SERVICE_TOKEN` 注入为 `GO_GATEWAY_INTERNAL_SERVICE_TOKEN`，Redis 凭据同样注入自 `REDIS_USERNAME`/`REDIS_PASSWORD`。

## 7. 镜像构建与获取

全量部署需要 7 个镜像（`<tag>` 以第 2 节的 `images` 段为准）：

```text
localhost/ironagent-admin:<tag>
localhost/ironagent-security-service:<tag>
localhost/go-gateway:<tag>
localhost/ironagent:<tag>
localhost/ironagent-callback-receiver:<tag>
mysql:8.4
redis:7.2.10-alpine
```

构建来源（均在 `main` 分支）：

| 镜像 | 构建来源 |
|---|---|
| `ironagent-admin` | `docker/Dockerfile.admin` |
| `ironagent-security-service` | `docker/Dockerfile.security-service` |
| `go-gateway` | `go-gateway/Dockerfile` |
| `ironagent` | `docker/Dockerfile.ironagent` |
| `ironagent-callback-receiver` | `docker/Dockerfile.callback-receiver` |
| `mysql:8.4` | 官方镜像 |
| `redis:7.2.10-alpine` | 官方镜像 |

构建完成后导出（`<release>` 为本次发布标识，`<tag>` 替换为实际 tag）：

```bash
mkdir -p /root/image-export/<release>
docker save -o /root/image-export/<release>/ironagent-admin-<tag>.tar localhost/ironagent-admin:<tag>
docker save -o /root/image-export/<release>/ironagent-security-service-<tag>.tar localhost/ironagent-security-service:<tag>
docker save -o /root/image-export/<release>/go-gateway-<tag>.tar localhost/go-gateway:<tag>
docker save -o /root/image-export/<release>/ironagent-<tag>.tar localhost/ironagent:<tag>
docker save -o /root/image-export/<release>/ironagent-callback-receiver-<tag>.tar localhost/ironagent-callback-receiver:<tag>
docker save -o /root/image-export/<release>/mysql-8.4.tar mysql:8.4
docker save -o /root/image-export/<release>/redis-7.2.10-alpine.tar redis:7.2.10-alpine
```

导入目标节点：

```bash
sudo mkdir -p /home/ubuntu/test
sudo ctr -n k8s.io images import /home/ubuntu/test/ironagent-admin-<tag>.tar
sudo ctr -n k8s.io images import /home/ubuntu/test/ironagent-security-service-<tag>.tar
sudo ctr -n k8s.io images import /home/ubuntu/test/go-gateway-<tag>.tar
sudo ctr -n k8s.io images import /home/ubuntu/test/ironagent-<tag>.tar
sudo ctr -n k8s.io images import /home/ubuntu/test/ironagent-callback-receiver-<tag>.tar
sudo ctr -n k8s.io images import /home/ubuntu/test/mysql-8.4.tar
sudo ctr -n k8s.io images import /home/ubuntu/test/redis-7.2.10-alpine.tar
sudo ctr -n k8s.io images ls | grep -E "<tag>"
```

如果 Pod 可能调度到多个 worker，必须在每个 worker 导入同一批镜像，或者改用私有镜像仓库。

## 8. 从头部署流程

### 8.1 预检

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf
kubectl get nodes -o wide
kubectl get sc
kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport \
  > /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml
bash scripts/check_single_entry_exposure.sh
```

期望：

- YAML 能渲染。
- 单入口检查通过。
- 渲染结果中只有 `go-gateway` 暴露 `nodePort: 30808`。

### 8.2 准备节点目录

在承载 local PV 的 worker 上执行：

```bash
sudo mkdir -p /app/ironagent/assistant-workspaces
sudo mkdir -p /app/ironagent/k8s-pv/mysql-data
sudo mkdir -p /app/ironagent/k8s-pv/redis-data-0 /app/ironagent/k8s-pv/redis-data-1
sudo chmod 0777 /app/ironagent/assistant-workspaces
```

### 8.3 清理旧部署

如需完整从头部署：

```bash
kubectl delete namespace ironagent --ignore-not-found
kubectl wait --for=delete namespace/ironagent --timeout=180s || true
```

注意：删除 namespace 不一定删除本地 PV 目录文件。若要清空业务数据，需要人工确认后再清理 hostPath 目录。

### 8.4 Apply

```bash
kubectl apply -f /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml
kubectl -n ironagent get pods -o wide
kubectl -n ironagent rollout status statefulset/mysql --timeout=300s
kubectl -n ironagent rollout status statefulset/redis-ha --timeout=300s
kubectl -n ironagent rollout status deploy/redis-sentinel --timeout=300s
```

### 8.5 数据库迁移

```bash
kubectl -n ironagent exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini upgrade head
'
```

确认核心表存在：

```bash
kubectl -n ironagent exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app .venv/bin/python - <<PY
import asyncio
from sqlalchemy import text
from src.shared.database import new_session

async def main():
    async with new_session() as session:
        for table in ["users", "assistants", "workspace_files", "workspace_file_events", "gateway_tasks", "gateway_hmac_nonces"]:
            result = await session.execute(text("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :table"), {"table": table})
            print(table, result.mappings().one()["n"])

asyncio.run(main())
PY
'
```

### 8.6 等待业务组件 ready

`admin` 与 `security-service` 的 readiness 会检查 workspace 文件 MySQL 元数据 schema；如果 8.5 未完成，下面两个 rollout 会卡住，这是预期的 fail-closed 门禁。

```bash
kubectl -n ironagent rollout status deploy/admin --timeout=300s
kubectl -n ironagent rollout status deploy/security-service --timeout=300s
kubectl -n ironagent rollout status deploy/go-gateway --timeout=300s
kubectl -n ironagent rollout status deploy/ironagent-gateway --timeout=300s
kubectl -n ironagent rollout status deploy/ironagent-cli --timeout=300s
kubectl -n ironagent rollout status deploy/ironagent-callback-receiver --timeout=300s
```

## 9. 部署后验证

```bash
curl -i "http://<PUBLIC_IP>:30808/healthz"
curl -i "http://<PUBLIC_IP>:30808/readyz"
curl -i "http://<PUBLIC_IP>:30808/api/v1/admin/bootstrap"
```

后台入口：

```text
http://<PUBLIC_IP>:30808/admin/
```

助手 UI 入口：

```text
http://<PUBLIC_IP>:30808/assistants/<assistant_id>/ui/chat?session=agent%3A<assistant_id>%3Amain
```

必须验证：

1. 后台能打开并加载助手列表。
2. 新建助手成功，重复名称被拦截。
3. 新建助手产生对应 runtime Pod。
4. 后台打开助手 UI 不再跳 404。
5. 助手 UI 能进入聊天页。
6. 工作空间只显示当前助手的 `persistent/`、`temp/` 和助手文件。
7. 上传、下载、删除文件后，workspace 目录、数据库 `workspace_files`、页面列表保持一致。
8. 重启助手 Pod 后，workspace 文件仍存在。
9. `/api/v1/chat` 返回 `202` 和 `task_id`。
10. `/api/v1/tasks/{task_id}` 能查询最终状态。
11. callback host 白名单正确时，上游 webhook 能收到事件。

## 10. 单端口验收

```bash
kubectl -n ironagent get svc -o wide
grep -n "nodePort:" /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml
```

期望：

- 只有 `go-gateway` 是 NodePort。
- `admin`、`security-service`、`ironagent-gateway`、`ironagent-callback-receiver`、`mysql`、`redis`、`redis-sentinel` 都是 ClusterIP。
- 公网只需要开放 `30808`。

## 11. 常见问题

| 问题 | 原因 | 修复 |
|---|---|---|
| `kubectl kustomize` 找不到 `prod-k8s-single-entry-nodeport` | 代码不是最新 `main` | 同步最新 `main` |
| 页面空白或 Admin 无响应 | 安全组未开放 `30808`，或 Gateway proxy 到 admin 失败 | 先在服务器本机 curl `127.0.0.1:30808/admin/`，再查 `go-gateway` 日志 |
| `device identity required` | 助手 Pod 配置为 provisioner 改默认禁用设备校验前的旧版本 | 重建助手 Pod 让 provisioner 重写配置（现默认写入 `dangerouslyDisableDeviceAuth`）；生产使用 HTTPS |
| 工作空间一直加载 | workspace API、allowed origin、runtime Pod、DB workspace_files 异常 | 查浏览器 Network、`go-gateway` 日志、`admin` 日志和 runtime Pod |
| `callback url host is not allowed` | callback host 未进白名单 | 测试可临时设置 private hosts；生产配置 `GO_GATEWAY_CALLBACK_ALLOWED_HOSTS` |
| Pod 拉不到镜像 | 节点 containerd 未导入 tar 或 tag 不一致 | 在实际调度节点导入镜像，核对 `ctr -n k8s.io images ls` |
| MySQL 表不存在 | 未执行 alembic migration | 执行第 8.5 节 |
| 助手 Pod 无法写 workspace | PV 目录权限、subPath 初始化或 UID/GID 不一致 | 检查 worker hostPath 权限和 Pod events |

## 12. 运维原则

- 线上热修只能短期验证，必须同步到代码、重打镜像、移除热修 ConfigMap。
- 不要把真实 Secret 写入 Git。
- local PV 只能保证固定节点可用，不等于多节点 HA。
- 生产多节点前必须切到 RWX 存储或对象存储应用层同步方案。
- 如果使用本地镜像 tar，所有可能调度 Pod 的节点都要导入镜像。
- 每次部署必须保存渲染后的 YAML、镜像 tag、Git commit 和测试报告。
