# IronAgent

> IronAgent 的 WebUI 与 assistant runtime 执行面，把 LLM、工具、技能和消息路由打包为可部署服务。

本分支与控制面 `main` 保持独立历史，通过 Go Gateway 的 `/assistants/{assistant_id}/...` 单入口协作。它包含聊天 UI、runtime Gateway 和助手运行时镜像源码。默认配置仅为示例，生产部署必须使用外部 Secret 管理真实凭据。

该执行面基于 MIT 许可的 OpenClaw 代码演进，原始版权和许可声明保留在 [LICENSE](LICENSE) 中。本分支继续按 MIT License 发布。

---

## 目录

- [概览](#概览)
- [快速开始](#快速开始)
- [部署](#部署)
- [使用](#使用)
- [LLM 配置](#llm-配置)
- [中文输出策略](#中文输出策略)
- [开发](#开发)
- [安全合规](#安全合规)
- [故障排查](#故障排查)
- [仓库地址](#仓库地址)

---

## 概览

IronAgent 是一个基于 Node.js / TypeScript 构建的 AI Agent 运行时,核心能力:

| 能力               | 说明                                                                                         |
| ------------------ | -------------------------------------------------------------------------------------------- |
| **LLM 调度**       | 通过 OpenAI 兼容协议接入 DashScope(默认 `MiniMax-M2.5`),或本地 Ollama / vLLM / SGLang        |
| **工具调用**       | 浏览器自动化、文件操作、shell 执行、PDF 阅读/编辑、Office 文档、内存检索 等 24 个核心 plugin |
| **Skill 系统**     | 23 个 ready-to-use skill,通过 SKILL.md 描述能力                                              |
| **Web Control UI** | 浏览器对话界面、设备配对、Workspace 管理                                                     |
| **CLI**            | `ironagent agent` / `models` / `plugins` / `skills` / `doctor` 等                            |
| **多设备**         | 通过 phone-control / device-pair 联动 macOS/iOS 节点                                         |
| **中文优先**       | 三层指令注入(workspace + system prompt hardcode)确保默认简体中文                             |

镜像约 **2.4 GB**,运行时只占用单进程 Node.js gateway + 必要的 Python 工具。

---

## 快速开始

### 前置条件

- Docker 24+(macOS 推荐 OrbStack)
- 一个可用的 DashScope(或其他 OpenAI 兼容)API Key

### 三步启动

```bash
# 1. 配置 .env
cp .env.example .env
$EDITOR .env
# 必填:
#   IRONAGENT_CONFIG_DIR=<absolute path, 例:$HOME/.ironagent>
#   IRONAGENT_WORKSPACE_DIR=<absolute path, 例:$HOME/.ironagent/workspace>
#   IRONAGENT_GATEWAY_TOKEN=<随机字符串,首次启动后会持久化到 config>
#   OPENAI_API_KEY=<DashScope API Key>
#   OPENAI_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
mkdir -p "$IRONAGENT_CONFIG_DIR" "$IRONAGENT_WORKSPACE_DIR"

# 2. 启动 gateway + cli 容器
docker compose up -d

# 3. 验证
curl http://localhost:18789/healthz
# 期望: {"ok":true,"status":"live"}
```

### 第一次对话

```bash
docker compose run --rm ironagent-cli agent --session-id smoke --message "你好,简要介绍 IronAgent。"
```

打开浏览器:**`http://localhost:18789/`** → 输入 `IRONAGENT_GATEWAY_TOKEN` 完成配对 → 在 UI 里聊天。

---

## 部署

### docker-compose 服务结构

| 服务                | 用途                                                                           | 状态             |
| ------------------- | ------------------------------------------------------------------------------ | ---------------- |
| `ironagent-gateway` | WebSocket Gateway + HTTP API + Control UI,常驻                                 | `unless-stopped` |
| `ironagent-cli`     | CLI 一次性运行容器,通过 `docker compose run --rm ironagent-cli <command>` 调用 | one-shot         |

### 暴露端口

| 端口    | 协议             | 用途                                  |
| ------- | ---------------- | ------------------------------------- |
| `18789` | HTTP + WebSocket | Gateway 主端口(Control UI + agent WS) |
| `18790` | TCP              | Bridge protocol(macOS/iOS 节点联动用) |

### Volumes

| 路径                                                         | 说明                                        |
| ------------------------------------------------------------ | ------------------------------------------- |
| `$IRONAGENT_CONFIG_DIR → /home/node/.ironagent`              | gateway 配置、agent workspace、session 日志 |
| `$IRONAGENT_WORKSPACE_DIR → /home/node/.ironagent/workspace` | agent 工作目录(skill output、scratch 文件)  |

### 首次连接的设备配对

Control UI 或 CLI 首次连接 gateway 会产生 pending 配对请求,管理员审批后才有完整权限:

```bash
docker compose run --rm ironagent-cli devices list      # 查看 pending 请求 ID
docker compose run --rm ironagent-cli devices approve <request-id>
# 或一键:
docker compose run --rm ironagent-cli devices approve --latest
```

---

## 使用

### Control UI

- URL:`http://localhost:18789/`
- 进入后输入 `IRONAGENT_GATEWAY_TOKEN`(`.env` 中或 `~/.ironagent/ironagent.json` 里 `gateway.auth.token`)
- 内置功能:对话、Session 历史、Workspace 文件浏览、设备列表

### CLI 主要命令

```bash
docker compose run --rm ironagent-cli <command> [options]
```

| 命令                                      | 说明                                                                       |
| ----------------------------------------- | -------------------------------------------------------------------------- |
| `agent --session-id <id> --message "..."` | 一次性 agent 对话                                                          |
| `tui`                                     | 终端 UI 交互模式(需 `-it` 加 `--rm` 不能并存,建议 attach 到 cli container) |
| `status`                                  | 整体状态概览(gateway / agents / memory / channels)                         |
| `doctor`                                  | 健康检查 + 修复建议                                                        |
| `models list / status / set <id>`         | 模型管理                                                                   |
| `plugins list / inspect <id>`             | Plugin 管理                                                                |
| `skills list / info <slug> / check`       | Skill 管理                                                                 |
| `devices list / approve <id>`             | 设备配对                                                                   |
| `gateway status / probe / call <method>`  | Gateway RPC                                                                |
| `config get/set/list`                     | 运行时配置                                                                 |
| `--version` / `--help`                    | 元信息                                                                     |

### 24 个内置 Plugin(精简后)

| 类别             | Plugin                                                                                                                 |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **核心运行时**   | `acpx`、`browser`、`device-pair`、`phone-control`、`ironagent-bridge`、`memory-core`、`shared`、`thread-ownership`     |
| **能力 Core**    | `image-generation-core`、`media-understanding-core`、`speech-core`、`talk-voice`                                       |
| **LLM Provider** | `openai`(DashScope OpenAI-compat 走这个)、`ollama`、`vllm`、`sglang`、`litellm`、`opencode`、`opencode-go`、`llm-task` |
| **功能**         | `diffs`(代码 diff viewer)、`diagnostics-otel`(OTel observability)、`memory-lancedb`(本地向量数据库)、`openshell`       |

> 已删除 66 个上游公网 LLM provider / IM 渠道 / 公网搜索 / TTS 等不适用的 plugin。详见 Phase 10.2 commit。

### 23 个内置 Skill(精简后)

| 类别          | Skill                                                                                                             |
| ------------- | ----------------------------------------------------------------------------------------------------------------- |
| **系统/健康** | `healthcheck`、`node-connect`、`session-logs`                                                                     |
| **工作流**    | `ironflow`、`ironflow-inbox-triage`、`canvas`                                                                     |
| **文档处理**  | `local-pdf`、`nano-pdf`、`office-docs`、`summarize`                                                               |
| **开发协作**  | `coding-agent`、`mcporter`(MCP 协议)、`oracle`(prompt 助手)、`model-usage`(成本统计)、`skill-creator`、`obsidian` |
| **通信/邮件** | `himalaya`(IMAP/SMTP)                                                                                             |
| **语音**      | `openai-whisper`(本地)、`openai-whisper-api`、`sherpa-onnx-tts`(本地 TTS)                                         |
| **其他**      | `1password`、`tmux`、`weather`                                                                                    |

> 已删除 32 个 macOS-personal、消费电子、公网 IM、SaaS-only 的 skill。详见 Phase 10.2 commit。

---

## LLM 配置

### 默认:DashScope OpenAI 兼容路径

`.env` 配置:

```ini
OPENAI_API_KEY=sk-sp-...           # DashScope API Key
OPENAI_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
```

`~/.ironagent/ironagent.json` 关键字段(由 gateway 启动时按需写入):

```json
{
  "agents": {
    "defaults": {
      "model": { "primary": "dashscope/MiniMax-M2.5" }
    }
  },
  "models": {
    "mode": "merge",
    "providers": {
      "dashscope": {
        "baseUrl": "https://coding.dashscope.aliyuncs.com/v1",
        "apiKey": "sk-sp-...",
        "auth": "api-key",
        "api": "openai-completions",
        "models": [
          {
            "id": "MiniMax-M2.5",
            "name": "MiniMax-M2.5",
            "api": "openai-completions",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
            "contextWindow": 200000,
            "maxTokens": 8192
          }
        ]
      }
    }
  }
}
```

### 备选:本地 LLM

- **Ollama**:`docker compose run --rm ironagent-cli models auth login --provider ollama`
- **vLLM / SGLang**:在 config 中加 provider 块,`baseUrl` 指向你的本地服务
- **LiteLLM proxy**:同上

### 切换默认模型

```bash
docker compose run --rm ironagent-cli models set dashscope/MiniMax-M2.5
docker compose run --rm ironagent-cli models status     # 验证 default
```

---

## 中文输出策略

IronAgent 默认以**简体中文**与用户对话(thinking、回复、错误说明、计划描述全部中文)。代码、命令、文件路径、API 字段、URL、git hash、专有名词保留原文。

### 三层注入(冗余防线)

| 层级                          | 文件                                                    | 作用                                           |
| ----------------------------- | ------------------------------------------------------- | ---------------------------------------------- |
| **A. host workspace**         | `~/.ironagent/agents/<id>/agent/{USER,SOUL,AGENTS}.md`  | 当 agent 读 workspace 时生效(TUI / 长 session) |
| **B. workspace templates**    | `docs/reference/templates/{USER,SOUL,AGENTS,CLAUDE}.md` | 新 agent workspace bootstrap 时继承            |
| **C. system prompt hardcode** | `src/agents/system-prompt.ts:~424`                      | **每次 LLM call 顶部注入**,one-shot 模式也覆盖 |

### 临时切换为其他语言

在当前 session 明确请求,如:

```
Reply in English: what can you do?
```

session 结束后自动回到简体中文。

### 修改默认指令

编辑 `src/agents/system-prompt.ts` 中的 `## 输出语言 / Output Language` 段,然后重新 build 镜像。

---

## 开发

### 仓库结构

```
.
├── apps/              # iOS / macOS / Android 原生 app(可选编译)
├── docs/              # 内部文档(reference templates + 各组件说明)
├── extensions/        # 24 个 plugin 源码(每个一个 workspace package)
├── skills/            # 23 个 skill SKILL.md + helper
├── src/               # core gateway / agent runtime / channels / config
├── ui/                # Control UI(Lit-based,生成 dist/control-ui)
├── scripts/           # build / postbuild / docker setup
├── Dockerfile         # 多阶段构建 + sanitize stage
├── docker-compose.yml
└── package.json       # pnpm monorepo root
```

### 常用命令

```bash
# 安装依赖
pnpm install

# 类型检查
pnpm tsgo

# 主仓库 lint + type + custom checks
pnpm check

# 单元测试
pnpm test

# 构建 dist(供 Docker 用)
pnpm build:docker

# 构建 Control UI bundle
pnpm ui:build

# Docker 镜像构建
DOCKER_BUILDKIT=1 docker build -t ironagent:local .
```

### Plugin SDK Facade 维护

新增/删除 plugin 后需重新生成 facade type map:

```bash
pnpm plugin-sdk:facades:gen
```

---

## 安全合规

仓库源码与镜像已通过严格 brand 合规扫描:**所有上游历史 brand 字面量(含历史前身和 fork-base 中间 brand)在源码和镜像中命中 0**。

### 已确认 0 残留(可复跑的扫描命令)

```bash
# 源码 brand pattern 扫描(由内部团队自定义关键词列表替换 <BRAND_REGEX>)
git ls-files | xargs grep -lEi "<BRAND_REGEX>" 2>/dev/null | wc -l    # 预期 0

# 镜像 brand pattern 扫描
docker run --rm ironagent:local sh -c '
  find /app -type f -exec grep -lEi "<BRAND_REGEX>" {} + 2>/dev/null
' | wc -l                                                              # 预期 0
```

具体扫描关键词清单(内部 brand-compliance 规范)与对应 audit 报告由内部安全团队维护。

### 允许的 false positive(请加入安全扫描 allowlist)

| 文件类型                                                                 | 来源                                      | 性质                               |
| ------------------------------------------------------------------------ | ----------------------------------------- | ---------------------------------- |
| `node_modules/psl/data/rules.js` 等                                      | Public Suffix List 标准数据               | 含波兰城市等 TLD,与产品 brand 无关 |
| `*.wasm` / `wasm-inlined.mjs` / `emscripten-module.WASM_RELEASE_SYNC.js` | 各 WASM 二进制                            | 编译产物中偶然 byte 序列           |
| `dist/extensions/diffs/assets/viewer-runtime.js`                         | Shiki diff viewer + ABAP TextMate grammar | 语法数据中偶然字符                 |
| `pandas>=2.1.0`(`Dockerfile`)                                            | Python 数据分析库依赖                     | 库名 substring 跟产品 brand 无关   |
| `expandable` UI 文案                                                     | i18n / CSS / TS                           | 单词内部偶然 substring             |

详见内部审计记录的 `Phase 10.4` / `Phase 12` 阶段提交日志。

### 内部 LLM API Key 处理

- API Key 通过 `.env` 注入环境变量(不入 git)
- 持久化到 `~/.ironagent/ironagent.json` 时被 secret 字段标识(查看时自动 redact)
- 不要把 `.env` 或 `~/.ironagent/ironagent.json` 提交到 git(`.gitignore` 已覆盖)

---

## 故障排查

| 问题                              | 排查                                                                                                                                                                |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `curl /healthz` 不通              | `docker compose ps` 看 gateway 状态;`docker compose logs ironagent-gateway` 看启动错误                                                                              |
| CLI 报 `pairing required`         | `docker compose run --rm ironagent-cli devices approve --latest`                                                                                                    |
| CLI 报 `gateway connect failed`   | 检查 `.env` 中 `IRONAGENT_GATEWAY_TOKEN` 跟 `~/.ironagent/ironagent.json` 的 `gateway.auth.token` 是否一致;不一致就用 config 里的                                   |
| Chat 返回英文                     | 1) Image 未含 Phase 11.1 的 system-prompt 改动 → 重 build;2) `~/.ironagent/agents/main/agent/USER.md` 缺失 → 从 `docs/reference/templates/USER.md` 复制过去         |
| `skills search` 报 `fetch failed` | `IronHub` 远程 marketplace 未在内网部署。skill 通过 `skills list` 看 bundled 即可,不需要 search                                                                     |
| Control UI 加载空白               | `dist/control-ui/index.html` 没生成 → 跑 `pnpm ui:build` 后 `docker build` 重建镜像;或者 docker-compose 把 `./dist:/app/dist` mount 取消(让 image 内置 UI 资源生效) |

### 重启 gateway

```bash
docker compose restart ironagent-gateway
# 或完全重启
docker compose down && docker compose up -d
```

### 重置 agent workspace

```bash
rm -rf $IRONAGENT_CONFIG_DIR/agents/main
docker compose restart ironagent-gateway
# gateway 启动时会从 docs/reference/templates 重新 bootstrap
```

---

## 仓库地址

- GitHub: `https://github.com/aiujdha/ironagent`
- 执行面分支: `feat/webui`
- 控制面分支: `main`
- Issue / 改进建议: 使用 GitHub Issues 和 Pull Requests
