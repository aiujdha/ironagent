# IronAgent 功能测试手册

## 1. 目的

这份文档用于指导测试人员按固定顺序验证当前 IronAgent 主线能力是否正常。

本文覆盖的能力包括：

1. Docker 控制面启动
2. 助手自动创建与自动起容器
3. Gateway URL / Gateway Token 生成
4. 共享工作空间初始化
5. 内部市场 `skills/plugins` 上传、下载、审核、安装
6. office 类 `skill/plugin` 的分步骤、分复杂度验证
7. 宿主机持久化目录写入
8. `admin` 与 `security-service` 共享同一份市场数据
9. 安全 hook 接口基本可用
10. 安全拦截、用户审批、管理员审批链路可验证
11. 后台通过一次性 launch ticket 安全入口打开 `SPDBPanda` 聊天页
12. 聊天页上传文件到助手工作空间并同步对象存储的链路

## 2. 测试前提

需要提前准备：

1. `ironagent` 仓库
2. `ironagent-main` 仓库
3. `ironagent-runtime:dev` 镜像
4. `docker compose` 可用
5. `.env` 已正确填写

推荐端口：

- `security-service`: `19000`
- `admin`: `19502`

## 3. 测试环境检查

执行：

```bash
docker version
docker compose version
curl http://127.0.0.1:19000/health
curl -I http://127.0.0.1:19502
```

期望结果：

1. Docker 命令正常返回
2. `security-service` 返回 `{"status":"ok"}`
3. `admin` 返回 `HTTP/1.1 200 OK`

## 4. 测试项

### 4.1 控制面启动

执行：

```bash
docker compose up -d --build security-service admin
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

期望结果：

1. `ironagent-security-service-1` 处于 `Up`
2. `ironagent-admin-1` 处于 `Up`

### 4.2 新建助手自动起容器

在 `admin` 页面创建一个新助手，或在容器内执行：

```bash
docker exec ironagent-admin-1 /app/.venv/bin/python -c "
from src.assistants.service import AssistantService
record = AssistantService().upsert_assistant(
    assistant_id='test-assistant',
    name='Test Assistant',
    capability_profile='onlyread',
)
print(record.runtime.container_name)
print(record.runtime.gateway_url)
print(record.runtime.gateway_token)
"
```

期望结果：

1. 新助手记录写入 `data/assistants/assistants.json`
2. 自动生成一个 `ironagent-assistant-...` 容器
3. 自动生成 `gateway_url`
4. 自动生成 `gateway_token`

### 4.3 工作空间初始化

检查：

```bash
find data/workspaces/test-assistant -maxdepth 2 -type d | sort
```

期望结果至少包含：

1. `temp/`
2. `persistent/`
3. `skills/`
4. `plugins/`
5. `USER.md`
6. `AGENTS.md`

### 4.4 Gateway 可访问

执行：

```bash
curl -I http://127.0.0.1:<gateway_port>
docker inspect ironagent-assistant-test-assistant --format '{{json .State.Health}}'
```

期望结果：

1. 根页面返回 `HTTP/1.1 200 OK`
2. 容器健康状态最终为 `healthy`

说明：

- IronAgent 网关刚启动时，Docker health 可能短暂显示 `starting`
- 只要容器日志已经出现 `listening on ws://0.0.0.0:18789`，通常说明网关已经开始监听

### 4.5 Skill 上传

准备一个 zip 包，包内至少包含：

1. `manifest.json`
2. `SKILL.md`

执行：

```bash
curl -X POST http://127.0.0.1:19000/api/v1/market/skills/upload \
  -F slug=test-skill \
  -F display_name='Test Skill' \
  -F owner=QA \
  -F version=1.0.0 \
  -F tags=e2e \
  -F file=@/path/to/test-skill.zip
```

期望结果：

1. 返回 `200`
2. 返回体中含 `id`
3. 返回体中 `status=pending`
4. 包文件落到 `data/market/packages/skills/...`

### 4.6 Plugin 上传

准备一个 zip 包，包内至少包含：

1. `manifest.json`
2. 插件主文件，例如 `plugin.py`

执行：

```bash
curl -X POST http://127.0.0.1:19000/api/v1/market/plugins/upload \
  -F slug=test-plugin \
  -F display_name='Test Plugin' \
  -F owner=QA \
  -F version=1.0.0 \
  -F tags=e2e \
  -F file=@/path/to/test-plugin.zip
```

期望结果：

1. 返回 `200`
2. 返回体中含 `id`
3. 返回体中 `status=pending`
4. 包文件落到 `data/market/packages/plugins/...`

### 4.7 审核与安装

执行：

```bash
curl -X POST http://127.0.0.1:19000/api/v1/market/skills/<skill_id>/approve
curl -X POST http://127.0.0.1:19000/api/v1/market/plugins/<plugin_id>/approve

curl -X POST http://127.0.0.1:19000/api/v1/market/skills/<skill_id>/install \
  -d assistant_id=test-assistant

curl -X POST http://127.0.0.1:19000/api/v1/market/plugins/<plugin_id>/install \
  -d assistant_id=test-assistant
```

期望结果：

1. 审核接口返回 `approved`
2. skill 安装到：
   - `data/workspaces/test-assistant/skills/<skill_slug>/`
3. plugin 安装到：
   - `data/workspaces/test-assistant/plugins/<plugin_slug>/`
4. `install_count` 变为 `1`

### 4.8 市场数据宿主机持久化

检查：

```bash
find data/market -maxdepth 4 -type f | sort
cat data/market/catalog.json
```

期望结果：

1. `catalog.json` 存在
2. skill 包存在于 `data/market/packages/skills/...`
3. plugin 包存在于 `data/market/packages/plugins/...`

### 4.9 `admin` 与 `security-service` 数据一致性

执行：

```bash
docker exec ironagent-admin-1 /app/.venv/bin/python -c "
from src.market.service import MarketService
svc = MarketService()
print([item.slug for item in svc.list_skills()])
print([item.slug for item in svc.list_plugins()])
"
```

期望结果：

1. `admin` 容器能读到 `security-service` 刚上传的 skill
2. `admin` 容器能读到 `security-service` 刚上传的 plugin

### 4.9.1 Market 下载能力

说明：

- 当前版本已支持：
  - `GET /api/v1/market/skills/<skill_id>/download`
  - `GET /api/v1/market/plugins/<plugin_id>/download`
- 后台 `Market` 页面也已提供下载按钮
- 若本机全局代理会拦截 `127.0.0.1`，优先在 `security-service` 容器内执行下载验证

推荐执行：

```bash
docker exec ironagent-security-service-1 /app/.venv/bin/python - <<'PY'
import hashlib
import httpx

skill_id = "<skill_id>"
plugin_id = "<plugin_id>"
client = httpx.Client(timeout=60.0)

skill = client.get(f"http://127.0.0.1:8000/api/v1/market/skills/{skill_id}/download")
plugin = client.get(f"http://127.0.0.1:8000/api/v1/market/plugins/{plugin_id}/download")

print("skill", skill.status_code, len(skill.content), hashlib.sha256(skill.content).hexdigest())
print("skill-disposition", skill.headers.get("content-disposition"))
print("plugin", plugin.status_code, len(plugin.content), hashlib.sha256(plugin.content).hexdigest())
print("plugin-disposition", plugin.headers.get("content-disposition"))
PY
```

期望结果：

1. 两个下载接口都返回 `200`
2. 返回头里存在 `content-disposition`
3. 下载得到的字节大小大于 `0`
4. 若同时保留上传原包，下载文件 `sha256` 应与原包一致

### 4.9.2 Office Skill / Plugin 分步骤、分复杂度测试

这一组测试建议使用独立助手，例如 `market-office-e2e-20260407`。

#### L1 基础层：office skill / plugin 上传

准备：

1. `office skill` 包：
   - 推荐直接使用 `ironagent-main/skills/office-docs` 打包后的 zip
2. `office plugin` 包：
   - 至少包含 `manifest.json`
   - 可包含 `plugin.py`

执行目标：

1. office skill 上传成功并返回 `status=pending`
2. office plugin 上传成功并返回 `status=pending`
3. 宿主机市场目录下出现对应归档文件

#### L2 完整性层：office skill / plugin 下载回读

执行目标：

1. 下载 `office skill` 包返回 `200`
2. 下载 `office plugin` 包返回 `200`
3. 下载内容的 `sha256` 与上传原包一致

#### L3 分发层：审核并安装到助手工作区

执行：

```bash
curl -X POST http://127.0.0.1:19000/api/v1/market/skills/<skill_id>/approve
curl -X POST http://127.0.0.1:19000/api/v1/market/plugins/<plugin_id>/approve

curl -X POST http://127.0.0.1:19000/api/v1/market/skills/<skill_id>/install \
  -d assistant_id=market-office-e2e-20260407

curl -X POST http://127.0.0.1:19000/api/v1/market/plugins/<plugin_id>/install \
  -d assistant_id=market-office-e2e-20260407
```

检查：

```bash
find data/workspaces/market-office-e2e-20260407/skills -maxdepth 2 -type f | sort
find data/workspaces/market-office-e2e-20260407/plugins -maxdepth 2 -type f | sort
```

期望结果：

1. `skills/<office-skill-slug>/` 已出现
2. `plugins/<office-plugin-slug>/` 已出现
3. 安装返回中的 `install_count` 已增加

#### L4 能力层：office skill 在助手容器内真实生成文档

执行：

```bash
docker exec ironagent-assistant-market-office-e2e-20260407 python3 \
  /home/node/.ironagent/workspace/skills/<office-skill-slug>/scripts/make_office_docs.py \
  --out-dir /home/node/.ironagent/workspace/temp/office-market-smoke \
  --title 'Office Market Smoke' \
  --body 'Generated via market-installed office skill.' \
  --stdout-json \
  --sheet-name 'Summary' \
  --headers 'Item,Status' \
  --row 'Office Skill,Installed' \
  --row 'Plugin Package,Installed'
```

检查：

```bash
find data/workspaces/market-office-e2e-20260407/temp/office-market-smoke -maxdepth 1 -type f | sort
```

期望结果：

1. 命令返回 `stdout-json`
2. 生成以下四个文件：
   - `agent-word.docx`
   - `agent-word.pdf`
   - `agent-sheet.xlsx`
   - `agent-sheet.pdf`
3. 宿主机共享工作空间中也能看到同名文件

#### L5 包可用层：office plugin 在助手容器内最小执行验证

说明：

- 当前这个测试验证的是“market 上传/下载/安装后，插件包已正确分发到助手工作区并可被容器访问”
- 它不是 IronAgent 官方插件运行时注册测试

执行：

```bash
docker exec ironagent-assistant-market-office-e2e-20260407 python3 -c "
import importlib.util, pathlib
path = pathlib.Path('/home/node/.ironagent/workspace/plugins/<office-plugin-slug>/plugin.py')
spec = importlib.util.spec_from_file_location('office_plugin', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.export_summary([{'a': 1}, {'a': 2}]))
"
```

期望结果：

1. 命令可成功执行
2. 输出类似：

```python
{'kind': 'office-export', 'rows': 2}
```

### 4.9.3 助手侧已装列表、停用、启用、复用

这一组测试建议直接在 `Admin -> Assistants` 页面完成，目标是验证：

1. 市场安装后的包，能在助手自己的列表里看到
2. 助手可以停用和重新启用某个已装包
3. 助手可以把一个已装包复用到另一个助手

#### L1 可见性层：助手已装列表

前置条件：

1. 目标助手已经通过 `Market -> Install to assistant` 安装过至少一个 `skill`
2. 目标助手已经通过 `Market -> Install to assistant` 安装过至少一个 `plugin`

执行步骤：

1. 打开 `Admin -> Assistants`
2. 选中目标助手
3. 找到 `Installed skills and plugins`

期望结果：

1. `Installed Skills` 中能看到对应 skill
2. `Installed Plugins` 中能看到对应 plugin
3. 表格里能看到：
   - `slug`
   - `name`
   - `version`
   - `status`
   - `install_path`

#### L2 停用层：Disable item

执行步骤：

1. 在 `Installed Skills` 或 `Installed Plugins` 中选中一个条目
2. 点击 `Disable item`

检查：

```bash
find data/workspaces/<assistant_id>/.ironagent-disabled -maxdepth 4 -type f | sort
```

期望结果：

1. 页面提示该条目已变为 `disabled`
2. 原来的：
   - `data/workspaces/<assistant_id>/skills/<slug>/`
   - 或 `data/workspaces/<assistant_id>/plugins/<slug>/`
   不再存在
3. 对应文件被移动到：
   - `data/workspaces/<assistant_id>/.ironagent-disabled/skills/<slug>/`
   - 或 `data/workspaces/<assistant_id>/.ironagent-disabled/plugins/<slug>/`

#### L3 恢复层：Enable item

执行步骤：

1. 继续选中刚才已经停用的条目
2. 点击 `Enable item`

期望结果：

1. 页面提示该条目已恢复为 `enabled`
2. 对应文件回到：
   - `data/workspaces/<assistant_id>/skills/<slug>/`
   - 或 `data/workspaces/<assistant_id>/plugins/<slug>/`
3. `.ironagent-disabled/.../<slug>/` 不再存在

#### L4 复用层：Reuse to assistant

准备：

1. 再准备一个目标助手 `assistant-b`
2. 源助手 `assistant-a` 已有一个已装的 skill 或 plugin

执行步骤：

1. 在 `Admin -> Assistants` 中选中 `assistant-a`
2. 在已装列表里选中一个条目
3. 在 `Reuse to assistant` 里选择 `assistant-b`
4. 点击 `Reuse to selected assistant`

检查：

```bash
find data/workspaces/<assistant-b>/skills -maxdepth 3 -type f | sort
find data/workspaces/<assistant-b>/plugins -maxdepth 3 -type f | sort
```

期望结果：

1. 页面提示复用成功
2. 目标助手工作空间里出现对应副本
3. 目标助手自己的 `Installed Skills` 或 `Installed Plugins` 页面里也能看到该条目
4. 如果目标助手原先存在同名停用副本，复用后应只保留一份新的启用副本

### 4.10 安全 Hook 冒烟

执行：

```bash
curl -X POST http://127.0.0.1:19000/api/v1/hooks/pre-execute \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": {"hook_name": "pre_execute"},
    "assistant": {"assistant_id": "test-assistant", "capability_profile": "onlyread"},
    "tool": {"name": "exec", "args": {"command": "ls ./workspace"}},
    "risk": {"level": "medium", "tags": ["workspace"]},
    "metadata": {"source": "functional-test"}
  }'
```

期望结果：

1. 返回 `200`
2. 返回体中存在 `decision`
3. 返回体中存在 `reason`

### 4.11 安全拦截与审批链路验证

这一组测试建议直接调用 IronAgent 的 `pre-execute` 接口，不依赖 IronAgent WebUI，结果最稳定，也最容易逐字段比对。

建议先定义：

```bash
export HOOK_URL=http://127.0.0.1:19000/api/v1/hooks/pre-execute
```

说明：

1. 为了验证“路径规则 / 命令规则”本身，下面的测试统一使用 `capability_profile=dangerous`
2. 如果你传入了一个已存在的 `assistant_id`，系统会优先使用助手真实配置里的权限等级；为了避免干扰，下面示例默认不传 `assistant_id`
3. 每次返回都建议重点检查：
   - `decision`
   - `reason`
   - `metadata.path_class`
   - `metadata.approval_scope`
   - `approval.required`
   - `approval.status`
   - `approval.approval_id`

#### 4.11.1 拒绝改写共享工作空间敏感文件

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-sensitive-workspace-write", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": { "name": "write", "args": { "path": "AGENTS.md", "content": "demo" }, "source": "core" },
    "risk": { "level": "low", "tags": ["security", "workspace"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "deny"`
2. `reason = "sensitive path is read-only"`
3. `metadata.path_class = "workspace_sensitive"`
4. `metadata.action_class = "write"`

#### 4.11.2 拒绝改写容器内固定敏感路径

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-container-sensitive-write", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": { "name": "write", "args": { "path": "/etc/hosts", "content": "127.0.0.1 demo" }, "source": "core" },
    "risk": { "level": "low", "tags": ["security", "container"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "deny"`
2. `reason = "sensitive path is read-only"`
3. `metadata.path_class = "container_sensitive"`
4. `metadata.normalized_path = "/etc/hosts"`

补充说明：

- 同类规则同样覆盖 `/app/**`、`/usr/local/bin/**`

#### 4.11.3 拒绝读取容器内密钥路径

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-container-secret-read", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": { "name": "read", "args": { "path": "/root/.ssh/id_rsa" }, "source": "core" },
    "risk": { "level": "low", "tags": ["security", "secret"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "deny"`
2. `reason = "secret path is not accessible"`
3. `metadata.path_class = "container_secret"`
4. `metadata.action_class = "read"`

#### 4.11.4 删除 `persistent/` 文件需要用户审批

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-persistent-delete", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": { "name": "exec", "args": { "command": "rm persistent/report.txt" }, "source": "core" },
    "risk": { "level": "low", "tags": ["security", "persistent"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "pending_approval"`
2. `reason = "persistent workspace deletion requires user approval"`
3. `metadata.approval_scope = "user"`
4. `metadata.path_class = "workspace_persistent"`
5. `approval.required = true`
6. `approval.status = "pending"`
7. `approval.approval_id` 非空

#### 4.11.5 删除 `temp/` 文件可直接放行

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-temp-delete", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": { "name": "exec", "args": { "command": "rm temp/demo.txt" }, "source": "core" },
    "risk": { "level": "low", "tags": ["security", "temp"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "allow"`
2. `reason = "workspace cleanup command allowed"`
3. `metadata.policy_source = "command_guard"`
4. `metadata.action_class = "delete"`

这条测试用于确认：

- 临时目录删除不拦截
- 持久化目录删除要走用户审批

#### 4.11.6 容器内调用外部 API 需要管理员审批

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-external-api", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": { "name": "exec", "args": { "command": "curl https://example.com" }, "source": "core" },
    "risk": { "level": "low", "tags": ["security", "external-api"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "pending_approval"`
2. `reason = "third-party command requires administrator approval"`
3. `metadata.approval_scope = "admin"`
4. `metadata.command = "curl https://example.com"`
5. `approval.required = true`
6. `approval.status = "pending"`
7. `approval.approval_id` 非空

这条测试就是“容器内调用容器外部 API 指令拦截”的直接验证方法。

#### 4.11.7 安装外部插件或第三方依赖需要管理员审批

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-plugin-install", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": {
      "name": "exec",
      "args": { "command": "cd /home/node/.ironagent/workspace && ironagent plugins add demo/plugin" },
      "source": "core"
    },
    "risk": { "level": "low", "tags": ["security", "plugin-install"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "pending_approval"`
2. `reason = "third-party command requires administrator approval"`
3. `metadata.approval_scope = "admin"`
4. `metadata.command` 中包含 `ironagent plugins add demo/plugin`
5. `approval.required = true`
6. `approval.status = "pending"`
7. `approval.approval_id` 非空

补充说明：

- `pip install ...`
- `npm install ...`
- `pnpm add ...`
- `wget ...`
- `docker ...`

这些同类第三方命令也应按管理员审批处理。

#### 4.11.8 容器内普通命令允许执行

执行：

```bash
curl -sS "$HOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{
    "envelope": { "request_id": "ft-benign-command", "hook_name": "pre_execute" },
    "assistant": { "capability_profile": "dangerous" },
    "session": { "session_id": "ft-session", "run_id": "ft-run", "channel": "cli" },
    "tool": { "name": "exec", "args": { "command": "ls temp" }, "source": "core" },
    "risk": { "level": "low", "tags": ["security", "benign"] },
    "metadata": { "origin": "functional-test" }
  }'
```

期望结果：

1. `decision = "allow"`
2. `reason = "command allowed inside container"`
3. `metadata.policy_source = "command_guard"`
4. `metadata.command = "ls temp"`

#### 4.11.9 审批结果查询方法

当返回 `pending_approval` 时，可以把上一步返回的 `approval.approval_id` 带入：

```bash
curl -sS http://127.0.0.1:19000/api/v1/approvals/<approval_id>
```

期望结果：

1. 返回体中存在 `approval_id`
2. `status` 初始为 `pending`
3. 如果审批完成，`status` 会变为 `approved` 或其他最终态
4. `approval_scope` 与前面 hook 返回的 `metadata.approval_scope` 一致

### 4.12 通过安全入口打开 SPDBPanda 聊天页（launch ticket）

当前推荐入口不是手工复制 `gateway url/token`，主流程为：

```text
admin 后台
→ 点击“打开 SPDBPanda 聊天页”
→ 浏览器携带一次性 launch ticket 打开聊天页
→ 聊天页向 security-service 兑换真实连接参数
→ 自动连接目标助手
```

原始 `gateway url/token` 仅保留在后台页面的调试折叠区中，不再作为主操作流程。

执行步骤：

1. 在 `Admin -> Assistants` 页面选择一个已处于 `running` 的助手
2. 在助手详情中点击“打开 SPDBPanda 聊天页”

期望结果：

1. 浏览器跳转到 `SPDBPanda` 页面
2. 地址栏中不应出现可直接肉眼读取的原始 `gateway token`
3. 页面能够自动建立连接，直接进入会话，不需要手工再粘贴 `gateway url/token`

补充说明：

1. launch ticket 是短时效一次性票据，不适合长期保留页面后再使用
2. 如果 ticket 已过期，页面会提示安全入口失效，返回后台重新点击一次按钮即可
3. 如果来源校验不通过，页面会提示安全入口校验失败

### 4.13 聊天页文件链路观察点

通过聊天页上传文件时，当前预期链路为：

```text
用户浏览器
→ SPDBPanda WebUI 上传文件
→ security-service 接收文件
→ 文件写入助手工作空间
→ 元数据写入工作空间文件元数据库（当前为 SQLite）
→ 文件同步到对象存储 COS
→ 聊天页展示文件列表
→ 用户可查看 / 下载
```

建议重点观察：

1. 上传成功后，工作台能看到文件
2. 助手可读取该文件
3. 下载返回的内容与原文件一致
4. 助手生成的新文件也能被发现；新文件不一定瞬时出现在页面中，必要时手动刷新工作台或重新打开聊天页
5. 如果上传后助手说找不到文件，优先检查文件是否出现在工作台列表、是否上传到了当前会话对应的助手，以及后端日志中是否有对象存储或元数据写入失败

## 5. 本轮实测结论

2026-04-07 本地全 Docker 实测已确认：

1. 控制面 `19000/19502` 可正常访问
2. 新助手 `market-e2e-20260407` 创建后自动起 IronAgent 容器
3. `gateway_url=http://127.0.0.1:55202`
4. `gateway_token=ironagent-local-market-e2e-20260407`
5. `skills/` 与 `plugins/` 都已成功上传、审核、安装测试包
6. `skills/plugins` 下载接口已返回 `200`，下载包 `sha256` 与上传原包一致
7. office market skill 已在助手容器内真实生成 `docx/xlsx/pdf`
8. office market plugin 已完成“安装后可访问、可导入、可最小执行”验证
9. `data/market/catalog.json` 与 `data/market/packages/...` 已真实落到宿主机
10. `admin` 容器已确认能读取同一份市场目录
11. `pre-execute hook` 返回 `200`

## 6. 常见问题

### 6.1 本机开了全局代理，访问 `127.0.0.1` 返回 `502`

现象：

- Python `requests`
- Postman
- 某些 IDE 插件

会把 `http://127.0.0.1:19000` 错误地转发到本地代理端口，例如 `127.0.0.1:7890`，然后返回 `502`

这类误判不只会影响 `19000`，也会影响：

- `http://127.0.0.1:19502`
- 助手自己的 `gateway_url`，例如 `http://127.0.0.1:55202`

处理方法：

1. 给本地地址加直连白名单：
   - `127.0.0.1`
   - `localhost`
2. 或者在 Python 里显式禁用环境代理：

```python
import requests
session = requests.Session()
session.trust_env = False
```

或者：

```python
import httpx
with httpx.Client(trust_env=False) as client:
    client.get("http://127.0.0.1:<gateway_port>/")
```

3. 如果你要直接探活某个助手的 `gateway_url`，优先使用不走代理的命令：

```bash
curl --noproxy '*' -I http://127.0.0.1:<gateway_port>
```

### 6.2 Docker CLI 卡住

建议先重启 Docker Desktop，再重新执行：

```bash
docker compose up -d --build security-service admin
```

### 6.3 Market 数据没有落到宿主机

需要确认当前 `docker-compose.yml` 已包含：

1. `IRON_AGENT_MARKET_STORE_PATH`
2. `IRON_AGENT_MARKET_PACKAGE_ROOT`
3. `IRON_AGENT_MARKET_UPLOAD_ROOT`

并且它们都指向 `IRON_AGENT_STACK_ROOT/data/market/...`

### 6.4 聊天页提示 `pairing required` 或安全入口失效

优先检查：

1. 是否走了旧入口而不是后台安全入口
2. launch ticket 是否已经失效；过期后回到后台重新点击入口按钮即可恢复
3. 当前 WebUI 与后端版本是否匹配
4. 助手是否仍处于 `starting`，以及 `security-service` 是否仍健康
