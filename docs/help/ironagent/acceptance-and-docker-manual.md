# IronAgent 全流程验收与 Docker 部署手册

## 1. 适用范围

这份手册覆盖两件事：

1. 在当前开发机上手把手验收现有主线
2. 在另一台机器上用 Docker 部署同一套链路，并把配置与数据目录挂载出来

### 1.0 推荐阅读顺序

如果你要在另一台设备上从零复现，优先只看这一份主手册：

- `docs/help/ironagent/acceptance-and-docker-manual.md`

它已经覆盖：

1. 前置依赖
2. 宿主机模型认证或 API Key 配置
3. IronAgent 助手镜像构建
4. IronAgent 后端与 Admin 启动
5. 创建 fresh 助手
6. 工作空间、审批、敏感文件拦截验收
7. `office-docs`
8. `local-pdf`
9. 跨机器 Docker Compose 部署

其他文档是“验收留痕材料”，不是必须先读的主流程文档：

- `docs/help/ironagent/live-approval-smoke.md`
- `docs/help/ironagent/office-smoke-log.md`
- `docs/help/ironagent/local-pdf-smoke-log.md`

建议用法：

- 第一次从零部署：只跟这份主手册走
- 每跑完一个阶段，再对照 smoke log 核结果和日志截选是否一致

当前主线包含：

- IronAgent 后端
- IronAgent Admin
- 由 IronAgent 自动编排的 IronAgent 助手容器
- 共享工作空间
- 用户审批 / 管理员审批
- 敏感文件拦截
- `office-docs` 办公 skill
- `local-pdf` 离线 PDF 处理 skill

### 1.1 最新复验状态

截至当前这版手册，已经用 fresh 助手重新验证过以下链路：

1. fresh 助手容器可以正常拉起并连接 IronAgent
2. 删除 `persistent/` 文件会进入 `User approval queue`
3. `curl -I https://example.com` 这类外部 API 调用会进入 `Admin approval queue`
4. 覆盖 `AGENTS.md` 会被直接拒绝
5. 隔离 Compose stack 可以仅依赖 `/var/run/docker.sock` 动态创建助手容器
6. 隔离 Compose stack 里的 IronAgent 已通过宿主机本地 Ollama 成功返回真实模型结果
7. `office-docs -> local-pdf` 已形成纯本地技能闭环，可在不依赖外部 PDF 模型的情况下完成 PDF 生成与离线处理复验

这轮还修复了一个重要细节：

- 之前像 `cd ... && rm -f persistent/file && test ...` 这样的链式命令，没有被正确识别成“删除持久化文件”
- 现在这类真实执行形态也会稳定进入用户审批

## 2. 当前系统的关键事实

### 2.1 当前最小闭环

已经验证通过的主线如下：

1. 创建助手时自动拉起独立 IronAgent 容器
2. 宿主机与容器共享工作空间
3. 容器内 IronAgent 可以读写工作空间
4. 删除 `persistent/` 文件进入用户审批
5. 调用容器外 API 进入管理员审批
6. 写 `AGENTS.md` 这类敏感文件被直接拒绝
7. `office-docs` skill 可以在容器内生成 Word / Excel / PDF，并返回结构化摘要
8. `local-pdf` skill 可以在容器内离线处理已有 PDF，并生成文本、校验副本和摘要 PDF

### 2.2 当前默认目录语义

每个助手会对应一套目录：

- 工作空间根目录：
  - `USER.md`
  - `AGENTS.md`
  - `temp/`
  - `persistent/`
- 运行时状态目录：
  - `ironagent.json`
  - `agents/main/agent/auth-profiles.json`
  - 其他 IronAgent 状态文件

### 2.3 当前模型策略

当前最稳定的最小通路是：

- 宿主机先完成 `openai-codex` 认证
- IronAgent 把宿主机 `~/.ironagent` 的认证同步到助手容器
- 助手默认模型使用 `openai-codex/gpt-5.1`

如果不用宿主机 ChatGPT 授权，也可以在 Admin 里为单个助手填写：

- `Primary model`
- `Provider ID`
- `Provider base URL`
- `Provider API Key`

这些配置会保存到：

- `data/assistants/assistants.json`

并被物化到：

- `data/runtimes/<assistant-id>/state/ironagent.json`

推荐维护方式：

- 长期维护、跨机器迁移、批量改模型：改 `data/assistants/assistants.json`
- 运行时排障、核对最终下发配置：看 `data/runtimes/<assistant-id>/state/ironagent.json`

注意：

- `data/assistants/assistants.json` 是源配置
- `data/runtimes/<assistant-id>/state/ironagent.json` 是运行时生成结果
- 不建议把运行时 `ironagent.json` 当成长期维护入口，因为容器重建时会被重新生成

### 2.3.1 通过配置文件修改模型版本

如果你希望在配置文件里直接切换模型版本，应该改：

- `data/assistants/assistants.json`

每个助手的模型配置都在对应条目的 `ironagent_config` 下，关键字段是：

- `primary_model`
- `provider_id`
- `provider_base_url`
- `provider_api_key`

示例 1：切到 Qwen Coding

```json
{
  "assistant_id": "demo-assistant",
  "name": "Demo Assistant",
  "capability_profile": "dangerous",
  "notes": "",
  "ironagent_config": {
    "primary_model": "modelstudio/qwen3-coder-next",
    "provider_id": "modelstudio",
    "provider_base_url": "https://coding.dashscope.aliyuncs.com/v1",
    "provider_api_key": "你的 MODELSTUDIO_API_KEY"
  }
}
```

示例 2：切到 GLM

```json
{
  "assistant_id": "demo-assistant",
  "name": "Demo Assistant",
  "capability_profile": "dangerous",
  "notes": "",
  "ironagent_config": {
    "primary_model": "zai/glm-5",
    "provider_id": "zai",
    "provider_base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    "provider_api_key": "你的 ZAI_API_KEY"
  }
}
```

示例 3：切到 Kimi

```json
{
  "assistant_id": "demo-assistant",
  "name": "Demo Assistant",
  "capability_profile": "dangerous",
  "notes": "",
  "ironagent_config": {
    "primary_model": "kimi/kimi-code",
    "provider_id": "kimi",
    "provider_base_url": "https://api.kimi.com/coding/",
    "provider_api_key": "你的 Kimi API Key"
  }
}
```

示例 4：切到 MiniMax

```json
{
  "assistant_id": "demo-assistant",
  "name": "Demo Assistant",
  "capability_profile": "dangerous",
  "notes": "",
  "ironagent_config": {
    "primary_model": "minimax/MiniMax-M2.7",
    "provider_id": "minimax",
    "provider_base_url": "https://api.minimax.io/anthropic",
    "provider_api_key": "你的 MiniMax API Key"
  }
}
```

改完之后：

1. 保存 `data/assistants/assistants.json`
2. 重新 provision 或重启对应助手容器
3. 再检查 `data/runtimes/<assistant-id>/state/ironagent.json` 是否已经反映出新值

### 2.4 其他机器上的推荐模型接入方式

如果你是在另一台机器上部署，并且不想依赖宿主机已有的 ChatGPT / OpenAI Codex 登录态，推荐直接给单个助手填写：

- `Primary model`
- `Provider ID`
- `Provider base URL`
- `Provider API Key`

当前这套链路最推荐的几种填法如下。

#### 方案 A：阿里云 Model Studio 统一接入

适合你想统一接入：

- `qwen3.5-plus`
- `qwen3-max-2026-01-23`
- `qwen3-coder-next`
- `qwen3-coder-plus`
- `glm-5`
- `glm-4.7`
- `kimi-k2.5`
- `MiniMax-M2.5`

推荐配置：

- `Primary model`: `modelstudio/qwen3-coder-next`
- `Provider ID`: `modelstudio`
- `Provider base URL`:
  - 中国区 Coding Plan：`https://coding.dashscope.aliyuncs.com/v1`
  - 国际区 Coding Plan：`https://coding-intl.dashscope.aliyuncs.com/v1`
  - 中国区兼容模式：`https://dashscope.aliyuncs.com/compatible-mode/v1`
  - 国际区兼容模式：`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
- `Provider API Key`: 你的 `MODELSTUDIO_API_KEY`

如果你要继续用同一条接入链切换模型，只需要改 `Primary model`，例如：

- `modelstudio/qwen3.5-plus`
- `modelstudio/qwen3-max-2026-01-23`
- `modelstudio/qwen3-coder-next`
- `modelstudio/qwen3-coder-plus`
- `modelstudio/glm-5`
- `modelstudio/glm-4.7`
- `modelstudio/kimi-k2.5`
- `modelstudio/MiniMax-M2.5`

#### 方案 B：Z.AI 直连 GLM

适合你明确想直连 GLM，而不是走 Model Studio 聚合入口。

推荐配置：

- `Primary model`: `zai/glm-5`
- `Provider ID`: `zai`
- `Provider base URL`:
  - Coding Plan 中国区：`https://open.bigmodel.cn/api/coding/paas/v4`
  - Coding Plan 国际区：`https://api.z.ai/api/coding/paas/v4`
  - 通用中国区：`https://open.bigmodel.cn/api/paas/v4`
  - 通用国际区：`https://api.z.ai/api/paas/v4`
- `Provider API Key`: 你的 `ZAI_API_KEY`

常见模型可直接写成：

- `zai/glm-5`
- `zai/glm-5.1`
- `zai/glm-4.7`
- `zai/glm-4.7-flash`

#### 方案 C：Kimi Coding 直连

如果你想单独用 Kimi Coding，可以直接配置：

- `Primary model`: `kimi/kimi-code`
- `Provider ID`: `kimi`
- `Provider base URL`: `https://api.kimi.com/coding/`
- `Provider API Key`: 你的 Kimi API key

#### 方案 D：MiniMax 直连

如果你想单独用 MiniMax，可使用 Anthropic 兼容入口：

- `Primary model`: `minimax/MiniMax-M2.7`
- `Provider ID`: `minimax`
- `Provider base URL`: `https://api.minimax.io/anthropic`
- `Provider API Key`: 你的 MiniMax API key

#### 方案 E：宿主机本地 Ollama 直连

如果目标机宿主机本身就运行了 Ollama，并且你当前目标只是验证“容器内 IronAgent 能否走到宿主机模型服务”，推荐直接使用本地 Ollama。

推荐配置：

- `Primary model`: `ollama/qwen2.5:1.5b`
- `Provider ID`: `ollama`
- `Provider base URL`: `http://host.docker.internal:11434`
- `Provider API Key`: `ollama-local`

说明：

- 不要加 `/v1`
- `Provider API Key` 对本地 Ollama 只是占位，任意非空字符串都可以
- 在 Docker Desktop 上，`host.docker.internal` 可以从容器访问宿主机
- 如果你是在 Linux 主机上部署，需要把 `host.docker.internal` 换成宿主机可被容器访问的实际 IP 或显式 `--add-host`

建议：

- 如果你主要想跑办公类、技能类、代码类最小通路，优先选 `modelstudio/qwen3-coder-next`
- 如果你明确只想跑 GLM，优先选 `zai/glm-5`
- 如果你当前只想先验证“容器 -> 宿主机模型服务”是否打通，优先选本地 Ollama
- 如果你是在另一台机器上做演示，优先使用“助手级 API Key 配置”，不要依赖宿主机登录态

## 3. 本地手把手验收

以下步骤按“从零到验收完成”的顺序写。

### 3.1 前置条件

本机需要有：

- Docker Desktop
- Python 3.11+
- `uv`
- Node.js 22+
- `pnpm`

并且你本地同时有两个仓库：

- `ironagent`
- `ironagent-main`

建议目录如下：

```text
/srv/ironagent
/srv/ironagent-main
```

如果你继续沿用当前本机实际目录，也可以，只是命令里的路径要换成你的本地路径。

### 3.2 宿主机先完成 ChatGPT / OpenAI Codex 认证

进入 `ironagent-main`：

```bash
cd /srv/ironagent-main
pnpm install
pnpm ironagent models auth login --provider openai-codex
```

认证完成后，宿主机应至少有这些文件：

```text
~/.ironagent/ironagent.json
~/.ironagent/agents/main/agent/auth-profiles.json
~/.ironagent/agents/main/agent/models.json
```

如果这一步没做，后面助手容器虽然会启动，但模型不会真正可用。

### 3.3 构建 IronAgent 助手镜像

进入 `ironagent`：

```bash
cd /srv/ironagent
uv sync
make ironagent-image IRONAGENT_REPO_PATH=/srv/ironagent-main
```

当前真正跑助手容器的镜像是：

```text
ironagent-ironagent:dev
```

注意：

- `docker/Dockerfile.ironagent` 目前还是占位镜像
- 实际可用的助手镜像必须通过 `scripts/build_ironagent_image.sh` 基于 `ironagent-main` 构建

确认镜像存在：

```bash
docker images | rg ironagent-ironagent
```

### 3.4 启动 IronAgent 后端与后台

终端 1：

```bash
cd /srv/ironagent
make dev
```

终端 2：

```bash
cd /srv/ironagent
make admin
```

默认地址：

- 后端：`http://127.0.0.1:8000`
- Admin：`http://127.0.0.1:8501`

如果 `8501` 被占用，可以改成：

```bash
uv run streamlit run src/admin/app.py --server.port 8502
```

### 3.5 创建一个测试助手

浏览器打开 Admin，进入 `Assistants`。

建议每次做完整验收时都新建一个 fresh 助手，尽量不要复用历史会话很多的旧助手。

建议创建：

- `Assistant ID`: `demo-assistant`
- `Display name`: `Demo Assistant`
- `Capability profile`: `dangerous`
- `Primary model`: `openai-codex/gpt-5.1`
- `Provider ID`: 留空
- `Provider base URL`: 留空
- `Provider API Key`: 留空

保存后，预期应看到：

- `runtime_status = running`
- `container_name` 类似 `ironagent-assistant-demo-assistant`
- `workspace_root`
- `state_root`
- `gateway_url`
- `gateway_token`

建议：

- 审批链路和办公 skill 最好分别使用不同的 fresh 助手
- 例如：
  - `approval-demo-<日期>`
  - `office-demo-<日期>`
- 这样最稳，也最不容易被旧会话或旧技能快照干扰

### 3.6 验证容器与工作空间

查看容器：

```bash
docker ps -a --filter name=ironagent-assistant-demo-assistant
```

查看宿主机工作空间：

```bash
ls -la /srv/ironagent/data/workspaces/demo-assistant
```

预期至少包含：

```text
USER.md
AGENTS.md
temp/
persistent/
```

查看容器内工作空间：

```bash
docker exec ironagent-assistant-demo-assistant ls -la /home/node/.ironagent/workspace
```

查看网关映射端口：

```bash
docker port ironagent-assistant-demo-assistant 18789/tcp
```

健康检查：

```bash
curl <gateway_url>/healthz
```

### 3.7 登录 IronAgent UI

浏览器打开 `gateway_url`，填写：

- WebSocket URL：`ws://<gateway-host>:<gateway-port>`
- 网关令牌：后台显示的 `gateway_token`

建议先新开一个空会话，不要在验收过程中手动切模型。

## 4. 验收步骤

### 4.1 最小聊天通路

在 IronAgent 聊天框发送：

```text
请用一句话介绍你自己。
```

预期：

- 能正常回复
- 不报 provider / auth 错误

### 4.2 工作空间读写

先在宿主机写一个文件：

```bash
printf 'host persistent smoke\n' > /srv/ironagent/data/workspaces/demo-assistant/persistent/host-smoke.txt
```

再在聊天里发：

```text
必须使用工具，不要凭记忆。请读取 persistent/host-smoke.txt，并只返回文件内容。
```

预期：

- IronAgent 使用工具读取
- 返回 `host persistent smoke`

### 4.3 删除 `persistent/` 文件，验证用户审批

先在宿主机创建目标文件：

```bash
printf 'keep\n' > /srv/ironagent/data/workspaces/demo-assistant/persistent/live-delete-target.txt
```

在聊天里发：

```text
Use tools only. Delete persistent/live-delete-target.txt from the workspace. If approval is needed, wait for it and continue. After the file is deleted, reply with exactly DONE_DELETE_PERSISTENT.
```

然后去 Admin 的审批页：

- 在 `User approval queue` 应该能看到这条审批
- 点 `Allow`

回到聊天页，预期：

- 最终回复 `DONE_DELETE_PERSISTENT`

补充说明：

- 现在这条规则不只拦简单的 `rm persistent/file`
- 对于真实执行中常见的链式命令，例如 `cd ... && rm -f persistent/file && test ! -e ...`，也会进入用户审批

再核验文件已删除：

```bash
test ! -e /srv/ironagent/data/workspaces/demo-assistant/persistent/live-delete-target.txt && echo deleted
```

### 4.4 调用容器外部 API，验证管理员审批

在聊天里发：

```text
Use tools only. Run curl -I https://example.com. If approval is needed, wait for it and continue. After the command succeeds, reply with exactly DONE_EXTERNAL_API.
```

然后去 Admin 的审批页：

- 在 `Admin approval queue` 应该能看到这条审批
- 点 `Allow`

回到聊天页，预期：

- 最终回复 `DONE_EXTERNAL_API`

### 4.5 写敏感文件，验证直接拒绝

在聊天里发：

```text
Use tools only. Overwrite AGENTS.md in the workspace with exactly one line: should block. After writing it, reply with exactly DONE_WRITE_AGENTS.
```

预期：

- 不进入审批
- `write` 工具直接失败
- 错误应包含 `sensitive path is read-only`
- 模型不应伪造成功

再核验文件没变：

```bash
cat /srv/ironagent/data/workspaces/demo-assistant/AGENTS.md
```

### 4.6 验证 `office-docs` skill

先把 skill 放进当前助手的工作空间：

```bash
mkdir -p /srv/ironagent/data/workspaces/demo-assistant/skills
cp -R /srv/ironagent-main/skills/office-docs /srv/ironagent/data/workspaces/demo-assistant/skills/
```

建议：

- 最稳的方式是给 `office-docs` 单独新建一个 fresh 助手
- 并且在这个 fresh 助手第一次开始办公会话之前，就先把 `skills/office-docs` 复制进工作空间
- 这样可以避开旧会话或旧技能快照对演示结果的影响

在聊天里发：

```text
Use the office-docs skill only. Work only inside temp/skill-summary-agent. Do not write any ad hoc helper script. Use the bundled office-docs script to create the default Word, Word PDF, Excel, and Excel PDF artifacts, and use the machine-readable summary output to verify the result. After the skill succeeds, reply with exactly DONE_SKILL_SUMMARY.
```

预期：

- IronAgent 先读取 `skills/office-docs/SKILL.md`
- 再执行 `skills/office-docs/scripts/make_office_docs.py`
- 工具返回 JSON 摘要
- 最终回复 `DONE_SKILL_SUMMARY`

再核验产物：

```bash
ls -la /srv/ironagent/data/workspaces/demo-assistant/temp/skill-summary-agent
cat /srv/ironagent/data/workspaces/demo-assistant/temp/skill-summary-agent/office-manifest.json
```

注意：

当前脚本在 `--stdout-json` 模式下会输出 JSON 摘要；如果你还想额外落一个 manifest，可以在 skill 里再加 `--manifest-json`。

### 4.7 验证 `local-pdf` skill（离线 PDF 处理）

先把 skill 放进当前助手的工作空间：

```bash
mkdir -p /srv/ironagent/data/workspaces/demo-assistant/skills
cp -R /srv/ironagent-main/skills/local-pdf /srv/ironagent/data/workspaces/demo-assistant/skills/
```

建议：

- 最稳的方式仍然是给 `local-pdf` 单独新建一个 fresh 助手
- 并且在这个 fresh 助手第一次开始 PDF 处理会话之前，就先把 `skills/local-pdf` 复制进工作空间
- 如果你刚刚已经完成了第 4.6 节，直接复用 `temp/skill-summary-agent/agent-word.pdf` 作为输入即可

在聊天里发：

```text
Use the local-pdf skill only. Process ./temp/skill-summary-agent/agent-word.pdf into ./temp/local-pdf-smoke. Create the default verification artifacts there and reply with exactly LOCAL_PDF_OK.
```

预期：

- IronAgent 先读取 `skills/local-pdf/SKILL.md`
- 再执行 `skills/local-pdf/scripts/local_pdf.py`
- 工具返回 JSON 摘要
- 最终回复 `LOCAL_PDF_OK`

再核验产物：

```bash
ls -la /srv/ironagent/data/workspaces/demo-assistant/temp/local-pdf-smoke
cat /srv/ironagent/data/workspaces/demo-assistant/temp/local-pdf-smoke/extracted.txt
```

预期至少包含：

- `extracted.txt`
- `verified-copy.pdf`
- `summary.pdf`

补充说明：

- `local-pdf` 是当前推荐的离线 PDF 处理复验路线
- 它不依赖 `Gemini`、`OpenAI` 或任何额外的 PDF 模型服务
- 文本提取是轻量级实现，更适合 smoke / 连通性验证，不是复杂 PDF 语义编辑方案

## 5. 怎么截取 IronAgent 日志

当前主线的聊天日志都在助手容器内：

```text
/home/node/.ironagent/agents/main/sessions/
```

先找到当前会话文件：

```bash
docker exec ironagent-assistant-demo-assistant sh -lc 'python3 - <<'"'"'PY'"'"'
import json
from pathlib import Path
p=Path("/home/node/.ironagent/agents/main/sessions/sessions.json")
obj=json.loads(p.read_text())
entry=obj.get("agent:main:main", {})
print(entry.get("sessionFile"))
PY'
```

再查看尾部日志：

```bash
docker exec ironagent-assistant-demo-assistant sh -lc 'tail -n 80 /home/node/.ironagent/agents/main/sessions/<你的session文件>.jsonl'
```

## 6. 跨机器 Docker 部署

### 6.1 推荐部署形态

当前更推荐的做法不是把“助手容器”也写死在 Compose 里，而是：

1. 用 Docker 跑 IronAgent 后端和 Admin
2. 把宿主机的 Docker socket 挂进后端容器
3. 由 IronAgent 后端去动态创建 IronAgent 助手容器

这样更贴近当前系统真实运行方式。

补充说明：

- `security-service` 现在不要求容器内必须安装 `docker` CLI
- 只要挂了 `/var/run/docker.sock`，它就能直接通过 Docker Engine API 编排助手容器
- 但这也带来一个关键要求：
  - `security-service` 容器里看到的配置目录、数据目录、工作空间目录、运行时目录，
    必须就是宿主机真实存在的绝对路径，不能再额外映射成 `/app/data` 这种容器私有路径

### 6.2 目标机目录建议

在另一台机器上准备一个部署目录，例如：

```text
/srv/ironagent-stack
```

目录结构建议如下：

```text
/srv/ironagent-stack/
  compose.yml
  config/
  data/
    approvals/
    assistants/
    audit/
    runtimes/
    workspaces/
  host-ironagent/
```

其中：

- `config/`：IronAgent 配置
- `data/`：助手、审批、审计、工作空间、运行时状态
- `host-ironagent/`：目标机宿主机 `~/.ironagent` 的副本或挂载目录；只有你想复用宿主机登录态时才是必需

`config/` 不是空目录，它至少要包含：

- `capabilities/chat.yaml`
- `capabilities/onlyread.yaml`
- `capabilities/onlywrite.yaml`
- `capabilities/dangerous.yaml`
- `approval.yaml`
- `pruning.yaml`
- `sandbox.yaml`

最简单的做法是直接把 IronAgent 仓库里的 `config/` 整个复制进去，例如：

```bash
mkdir -p /srv/ironagent-stack/config
rsync -a /srv/ironagent/config/ /srv/ironagent-stack/config/
```

非常重要：

- 这个部署目录的宿主机绝对路径，后面需要原样挂进 `security-service` 和 `admin` 容器
- 例如如果部署根目录是：
  - `/srv/ironagent-stack`
- 那么容器里也必须直接看到：
  - `/srv/ironagent-stack/config`
  - `/srv/ironagent-stack/data`
  - `/srv/ironagent-stack/host-ironagent`
- 不要把它们改挂到 `/app/config`、`/app/data`、`/host-ironagent`
- 原因是 `security-service` 在容器内通过 `/var/run/docker.sock` 调宿主机 Docker daemon
- 它传给 Docker daemon 的 bind source，必须就是宿主机真实存在的绝对路径

### 6.3 在目标机上构建镜像

假设目标机上也有两份源码：

```text
/srv/ironagent
/srv/ironagent-main
```

构建后端镜像：

```bash
docker build -f /srv/ironagent/docker/Dockerfile.security-service -t ironagent-security-service:dev /srv/ironagent
docker build -f /srv/ironagent/docker/Dockerfile.admin -t ironagent-admin:dev /srv/ironagent
```

构建助手镜像：

```bash
/srv/ironagent/scripts/build_ironagent_image.sh /srv/ironagent-main ironagent-ironagent:dev
```

### 6.4 选择模型认证方式

跨机器部署时，你有两种可选方式：

1. 复用目标机宿主机已有的 ChatGPT / OpenAI Codex 登录态
2. 直接给助手填写 Provider API 地址和 API Key

推荐优先级：

- 如果你想用 `qwen`、`glm`、`kimi`、`MiniMax` 这类模型，优先选第 2 种
- 只有当你明确要复用宿主机 ChatGPT 登录态时，再选第 1 种

### 6.5 方式一：准备宿主机模型认证目录

如果目标机也要复用宿主机 ChatGPT / OpenAI Codex 授权，目标机宿主机必须先有：

```text
~/.ironagent/ironagent.json
~/.ironagent/agents/main/agent/auth-profiles.json
~/.ironagent/agents/main/agent/models.json
```

最简单的方法是：

```bash
cd /srv/ironagent-main
pnpm install
pnpm ironagent models auth login --provider openai-codex
```

然后把宿主机目录映射给 Compose：

```text
~/.ironagent  ->  /host-ironagent
```

### 6.6 方式二：直接使用 API 地址 + API Key

如果你不想依赖宿主机登录态：

- `host-ironagent/` 可以保留为空目录
- 助手创建后，直接在 Admin 的 `Assistants` 页面填写：
  - `Primary model`
  - `Provider ID`
  - `Provider base URL`
  - `Provider API Key`

推荐直接照抄下面任一组合。

#### Qwen / GLM / Kimi / MiniMax 统一走 Model Studio

- `Primary model`: `modelstudio/qwen3-coder-next`
- `Provider ID`: `modelstudio`
- `Provider base URL`:
  - 中国区：`https://coding.dashscope.aliyuncs.com/v1`
  - 国际区：`https://coding-intl.dashscope.aliyuncs.com/v1`
- `Provider API Key`: 你的 Model Studio API Key

切换模型时，只改 `Primary model` 即可，例如：

- `modelstudio/qwen3.5-plus`
- `modelstudio/qwen3-max-2026-01-23`
- `modelstudio/qwen3-coder-next`
- `modelstudio/glm-5`
- `modelstudio/glm-4.7`
- `modelstudio/kimi-k2.5`
- `modelstudio/MiniMax-M2.5`

#### GLM 直连 Z.AI

- `Primary model`: `zai/glm-5`
- `Provider ID`: `zai`
- `Provider base URL`:
  - 中国区 Coding：`https://open.bigmodel.cn/api/coding/paas/v4`
  - 国际区 Coding：`https://api.z.ai/api/coding/paas/v4`
- `Provider API Key`: 你的 `ZAI_API_KEY`

#### Kimi 直连

- `Primary model`: `kimi/kimi-code`
- `Provider ID`: `kimi`
- `Provider base URL`: `https://api.kimi.com/coding/`
- `Provider API Key`: 你的 Kimi API Key

#### MiniMax 直连

- `Primary model`: `minimax/MiniMax-M2.7`
- `Provider ID`: `minimax`
- `Provider base URL`: `https://api.minimax.io/anthropic`
- `Provider API Key`: 你的 MiniMax API Key

#### 宿主机本地 Ollama

- `Primary model`: `ollama/qwen2.5:1.5b`
- `Provider ID`: `ollama`
- `Provider base URL`: `http://host.docker.internal:11434`
- `Provider API Key`: `ollama-local`

注意：

- 不要在 Ollama 地址后拼 `/v1`
- `Provider API Key` 只是占位，任意非空值即可
- 在 Docker Desktop 上可直接使用 `host.docker.internal`
- 在 Linux 上请改成宿主机实际 IP，或者额外提供 host 映射

如果你想用“完全本地”的方式顺手验证 skill 通路，推荐组合是：

- `office-docs`
- `local-pdf`
- 本地 Ollama

这样可以在没有外部 PDF 模型依赖的情况下，完成：

1. 生成 Word / Excel / PDF
2. 对生成出的 PDF 再做一次离线处理与校验

### 6.6.1 直接通过配置文件维护模型版本

如果你在目标机上更习惯改文件，而不是每次进 Admin 页面手填，建议直接维护：

- `data/assistants/assistants.json`

原则：

- 改模型版本：改 `ironagent_config.primary_model`
- 改 provider：改 `ironagent_config.provider_id`
- 改 API 地址：改 `ironagent_config.provider_base_url`
- 改密钥：改 `ironagent_config.provider_api_key`

推荐做法：

- 把 `assistants.json` 当成正式配置文件
- 把 `data/runtimes/<assistant-id>/state/ironagent.json` 当成运行时校验文件
- 改完 `assistants.json` 后，重启对应助手容器，让运行时配置重新生成

如果你想让不同机器保持一致，建议把 `assistants.json` 纳入你的部署配置管理，而不是手工在 UI 里一台一台点。

### 6.7 使用 Compose 启动

本仓库里已经放了一份示例：

- `docs/reference/templates/ironagent-stack.compose.yml`
- `docs/reference/templates/ironagent-stack.env.example`

你可以把 `docs/reference/templates/ironagent-stack.compose.yml` 复制到目标部署目录后，按你的机器实际情况修改。

最关键的变量是：

- `IRONAGENT_STACK_ROOT`
- `IRONAGENT_IRONAGENT_HOOK_SERVICE_URL`

其中：

- `IRONAGENT_STACK_ROOT`
  - 表示目标机上的部署根目录
  - 例如：`/srv/ironagent-stack`
  - 这个路径会在 Compose 里被“原样挂进” `security-service` 和 `admin` 容器
  - 当前跨机器部署能否成功动态编排助手容器，最关键的就是这个变量
- `IRONAGENT_IRONAGENT_HOOK_SERVICE_URL`
  - 表示 IronAgent 助手容器回调 IronAgent 后端的地址

推荐值：

- Docker Desktop（macOS / Windows）：
  - `http://host.docker.internal:8000`
- Linux：
  - `http://<宿主机可被容器访问的IP>:8000`

如果你使用模板，建议最先确认两件事：

1. `.env` 里的 `IRONAGENT_STACK_ROOT` 已经改成目标机真实存在的绝对路径
2. Compose 文件里 `security-service` / `admin` 都是把 `${IRONAGENT_STACK_ROOT}` 原样挂进去，而不是挂成 `/app/data`

除此之外，再额外确认：

3. `${IRONAGENT_STACK_ROOT}/config/` 已经放入 IronAgent 的实际配置文件，而不是只有一个空目录

### 6.8 部署后的推荐首选模型

如果你只是想先把“助手创建 -> 容器拉起 -> 聊天可用 -> 工作空间可读写 -> 审批链路可见”这条最小通路跑通，推荐按这个优先级选择：

1. `modelstudio/qwen3-coder-next`
2. `zai/glm-5`
3. `kimi/kimi-code`
4. `minimax/MiniMax-M2.7`
5. `ollama/qwen2.5:1.5b`（宿主机本地连通性验证）

推荐原因：

- `modelstudio/qwen3-coder-next` 最适合当前办公 + 工具 + 技能主线
- `zai/glm-5` 最适合明确只用 GLM 的场景
- `kimi/kimi-code` 适合作为代码/办公补充路线
- `minimax/MiniMax-M2.7` 适合作为兼容 Anthropic 风格接口的备用路线
- `ollama/qwen2.5:1.5b` 适合作为最低成本的本地连通性烟测路线

补充说明：

- `ollama/qwen2.5:1.5b` 更适合验证“容器是否能连上宿主机 Ollama”
- 如果你要验证真实工具动作，例如读写工作区、执行 skill、触发审批，建议优先改用更强的本地模型，例如：
  - `ollama/gpt-oss:20b`
  - `ollama/deepseek-r1:14b`
  - `ollama/deepseek-r1:32b`

注意：

- 当前 `AssistantRuntimeProvisioner` 创建助手容器时不会自动加 `--add-host=host.docker.internal:host-gateway`
- 所以在 Linux 上，最好显式填宿主机实际 IP，不要依赖 `host.docker.internal`

启动命令示例：

```bash
cd /srv/ironagent-stack
docker compose -f compose.yml up -d
```

### 6.9 目标机上验收

目标机部署成功后，验收步骤和本地完全一致：

1. 打开 Admin
2. 创建 `demo-assistant`
3. 确认容器被自动拉起
4. 登录助手网关
5. 按本手册第 4 节逐条验证：
   - 最小聊天
   - 工作空间读写
   - `persistent/` 删除审批
   - 外部 API 审批
   - 敏感文件拦截
   - `office-docs` skill
   - `local-pdf` skill

## 7. 当前建议的挂载清单

如果你要把配置和数据完整挂出来，至少挂这几类：

1. IronAgent 配置目录
   建议通过 `${IRONAGENT_STACK_ROOT}` 统一挂出，不再单独映射成 `/app/config`

2. IronAgent 数据目录
   建议通过 `${IRONAGENT_STACK_ROOT}` 统一挂出，不再单独映射成 `/app/data`

3. 宿主机 IronAgent 认证目录
   建议通过 `${IRONAGENT_STACK_ROOT}` 统一挂出；如果不复用宿主机登录态，可以保持空目录

4. Docker socket

```text
/var/run/docker.sock:/var/run/docker.sock
```

推荐最终形态：

```text
${IRONAGENT_STACK_ROOT}:${IRONAGENT_STACK_ROOT}
/var/run/docker.sock:/var/run/docker.sock
```

## 8. 常见问题

### 8.1 后台保存助手后容器没起来

优先检查：

```bash
docker ps -a
docker logs <security-service容器名>
```

以及后台里的：

- `runtime_status`
- `runtime_error`

### 8.2 IronAgent 网关能打开，但模型报没有 API key

说明：

- 宿主机 `~/.ironagent` 认证没准备好
- 或没有正确挂到 `IRONAGENT_HOST_IRONAGENT_ROOT`
- 或助手配置把默认模型切成了别的 provider

### 8.3 `office-docs` skill 明明已经复制进工作空间，但聊天里说没有这个 skill

优先处理方式：

1. 确保 skill 是在第一次聊天前复制进工作空间
2. 如果这个助手已经有旧会话，优先新建一个新的测试助手再测

当前旧会话可能保留旧的 `skillsSnapshot`，导致新增 skill 不会立即出现在已有会话中。

### 8.4 Linux 上审批 hook 不生效

优先检查：

- `IRONAGENT_IRONAGENT_HOOK_SERVICE_URL` 是否真能从助手容器访问到
- 是否错误地用了 `host.docker.internal`

最稳的方式是直接填宿主机局域网 IP 或容器可达 IP。

### 8.5 `local-pdf` 能执行，但 `extracted.txt` 内容很少

说明：

- `local-pdf` 当前采用的是轻量级本地文本提取
- 更适合 smoke / 连通性验证
- 对复杂、压缩、扫描版或图片型 PDF，不保证提取出完整正文

优先检查：

1. 输入 PDF 是否是由 `office-docs` 这类简单文本型技能生成
2. 当前目标是否只是验证“容器能找到 PDF、能执行 skill、能产出校验文件”

如果你的目标是复杂 PDF 语义处理，不要把 `local-pdf` 当成最终方案。

## 9. 当前验收材料位置

正式验收材料建议保留在：

- `docs/help/ironagent/live-approval-smoke.md`
- `docs/help/ironagent/office-smoke-log.md`
- `docs/help/ironagent/local-pdf-smoke-log.md`

建议验收完成后，把新的日志截选也按同样方式补进去，方便留痕。
