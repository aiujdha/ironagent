# 另一台机器从零部署到完整验收手册

这份文档是当前项目的**详细版主手册**。

如果你想看更短、更偏操作步骤的小白版，请看：

- `docs/beginner-docker-deploy-to-use.zh-CN.md`

如果你想做标准化验收，请看：

- `docs/functional-test.zh-CN.md`

这份手册面向“换一台机器，从零把 IronAgent 全部跑在 Docker 里，并完成主线验收”的场景。

本文默认采用这套运行方式：

- `security-service` 走 Docker 容器
- `admin` 走 Docker 容器
- 每个 IronAgent 助手实例由 IronAgent 动态编排为独立 Docker 容器

也就是说，控制面和执行面都走 Docker；你不需要再单独在宿主机启动 `make dev` 或 `make admin`。

## 1. 一句话流程

从零复现当前项目，按下面这条主线走：

1. 准备 `ironagent` 和 `ironagent-main`
2. 安装依赖
3. 构建 `ironagent-runtime:dev`
4. 配置 `.env`
5. `docker compose up --build -d`
6. 打开 Streamlit 后台
7. 创建两个助手
   - 一个走 `host_iron_agent`
   - 一个走 `provider_api_key`
8. 验证聊天连通性
9. 验证工作空间
10. 验证审批与拦截
11. 验证 `office-docs`
12. 验证 `local-pdf`

如果你只想先确认“全 Docker 运行是否成功”，做到第 12 节里的最小聊天和工作空间验证即可。

## 2. 你会完成什么

只跟这一份文档，你可以从零完成下面这些事情：

1. 准备仓库和依赖
2. 构建 IronAgent 助手镜像
3. 用 `docker compose` 启动 IronAgent 控制面
4. 通过 Streamlit 后台创建助手
5. 配置两种模型接入方式
   - 宿主机 IronAgent 认证
   - Provider API Key
6. 打通容器内 IronAgent 的最小聊天连通性
7. 验证共享工作空间挂载
8. 验证审批与拦截
9. 验证 `office-docs`
10. 验证 `local-pdf`

如果你只想快速确认“全 Docker 路径能不能起来”，做到第 8 节即可。  
如果你想做完整人工验收，建议一路做到第 10 节。

## 3. 平台建议

### 3.1 macOS / Linux

推荐直接在宿主机终端中执行本文命令。

### 3.2 Windows

全 Docker 方案在 Windows 上推荐使用 **WSL2**。  
原因很简单：当前 Compose 方案依赖 Linux 风格绝对路径的同路径挂载，原生 `C:\...` 路径不适合作为默认主线。

推荐方式：

1. 安装并启用 WSL2
2. 安装一个 Linux 发行版，例如 Ubuntu
3. 把 `ironagent` 和 `ironagent-main` 都克隆到 WSL2 文件系统内
4. 在 WSL2 终端里执行本文所有命令

Windows 宿主机常用命令：

```powershell
wsl --status
wsl -l -v
wsl
```

如果你已经进入 WSL2，后续就直接按本文的 Linux 命令执行。

## 4. 目录约定

推荐目录结构：

```text
/path/to/ironagent
/path/to/ironagent-main
```

示例：

```text
/Users/yourname/work/ironagent
/Users/yourname/work/ironagent-main
```

Windows + WSL2 示例：

```text
/home/yourname/work/ironagent
/home/yourname/work/ironagent-main
```

本文后面用这两个占位目录：

- IronAgent：`/path/to/ironagent`
- IronAgent：`/path/to/ironagent-main`

请替换成你的实际路径。

## 5. 前置检查

先确认工具都可用。

```bash
python3 --version
uv --version
node --version
docker version
docker compose version
```

如果你准备用宿主机本地 Ollama 做 Provider API Key 验证，再确认：

```bash
ollama list
curl http://127.0.0.1:11434/api/tags
```

建议宿主机至少准备一个本地 Ollama 模型，例如：

- `qwen2.5:1.5b`

说明：

- `ollama/qwen2.5:1.5b` 适合作为“全 Docker 路径是否打通”的默认烟测模型
- `ollama/gpt-oss:20b` 在资源更充足的机器上更适合严格口径验收
- 如果你的机器内存较紧，优先用 `qwen2.5:1.5b` 跑完整套主线

## 6. 安装仓库依赖

### 6.1 IronAgent

```bash
cd /path/to/ironagent
uv sync
```

### 6.2 IronAgent

```bash
cd /path/to/ironagent-main
pnpm install
```

## 7. 构建 IronAgent 助手镜像

回到 IronAgent 仓库。

```bash
cd /path/to/ironagent
make ironagent-image IRON_AGENT_REPO_PATH=/path/to/ironagent-main
```

如果你的环境没有 `make`，也可以直接构建：

```bash
docker build -t ironagent-runtime:dev /path/to/ironagent-main
```

构建完成后确认镜像存在：

```bash
docker images | grep ironagent-runtime
```

期望看到类似：

```text
ironagent-runtime   dev   ...
```

额外说明：

- 如果这一步在 Docker Desktop 上卡在 `ironagent-main` 的构建阶段，并出现 `SIGKILL`、`Killed`、`OOM` 等现象，通常是 Docker 分配的内存不够。
- 在资源较紧的机器上，建议先提高 Docker Desktop 的内存上限。
- 如果你本机已经有一个可用的 `ironagent-runtime:dev`，也可以先复用已有镜像，把主链路验收做完后再回头处理镜像重打。

## 8. 准备 `.env`

在 IronAgent 仓库根目录复制配置模板：

```bash
cd /path/to/ironagent
cp .env.example .env
```

然后编辑 `.env`，至少填这几项：

```env
IRON_AGENT_STACK_ROOT=/path/to/ironagent
IRON_AGENT_RUNTIME_SOURCE_ROOT=/path/to/ironagent-main
IRON_AGENT_HOST_IRON_AGENT_ROOT=/path/to/host/.ironagent
IRON_AGENT_SECURITY_SERVICE_PORT=8000
IRON_AGENT_ADMIN_PORT=8501
IRON_AGENT_RUNTIME_HOOK_SERVICE_URL=http://host.docker.internal:8000
IRON_AGENT_RUNTIME_CONTAINER_EXTRA_HOST=host.docker.internal:host-gateway
IRON_AGENT_RUNTIME_SEED_WORKSPACE_SKILLS=office-docs,local-pdf
```

说明：

- `IRON_AGENT_STACK_ROOT`
  - 必须是宿主机上 `ironagent` 仓库的**绝对路径**
- `IRON_AGENT_RUNTIME_SOURCE_ROOT`
  - 必须是宿主机上 `ironagent-main` 仓库的**绝对路径**
  - 当前用来把 `office-docs` 和 `local-pdf` 自动预置到 fresh 助手工作区
- `IRON_AGENT_HOST_IRON_AGENT_ROOT`
  - 如果你要用“宿主机 IronAgent 认证”，就填宿主机 `.ironagent` 目录的绝对路径
  - 如果你只打算验证 `Provider API Key`，也建议先保留一个真实路径，避免后续切换时再改 Compose
- `IRON_AGENT_RUNTIME_HOOK_SERVICE_URL`
  - 这是受管助手容器回调 IronAgent 后端时使用的地址
- `IRON_AGENT_RUNTIME_CONTAINER_EXTRA_HOST`
  - 默认值不要改，当前依赖它把 `host.docker.internal` 映射进受管容器

如果宿主机必须依赖代理或 VPN 才能访问外部模型，还建议继续补这几项：

```env
IRON_AGENT_RUNTIME_HTTP_PROXY=http://host.docker.internal:7890
IRON_AGENT_RUNTIME_HTTPS_PROXY=http://host.docker.internal:7890
IRON_AGENT_RUNTIME_NO_PROXY=localhost,127.0.0.1,host.docker.internal
```

说明：

- 这是给“助手容器里的 IronAgent”使用的，不只是给浏览器或宿主机终端使用
- 如果你本机代理端口是 `7890`，在容器里不要写 `127.0.0.1:7890`
- 要写成 `host.docker.internal:7890`
- 如果漏掉这一步，助手侧典型报错会是：
  - `LLM request failed: network connection error.`
  - 或者模型请求超时、连接失败
- `NO_PROXY` 至少建议包含：
  - `localhost`
  - `127.0.0.1`
  - `host.docker.internal`
  - 否则容器访问本地 Hook 服务时可能会被错误转发到代理

如果你希望用户在 Streamlit `Assistants` 页面里自由选择不同的公共工作空间根目录，还可以额外配置：

```env
IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON=[{"id":"team-a","label":"团队A工作空间","host_path":"/path/to/ironagent/data/workspaces-team-a"}]
IRON_AGENT_DEFAULT_WORKSPACE_ROOT_ID=default
```

说明：

- `IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON`
  - 是一个 JSON 数组，每一项都需要 `id`、`label`、`host_path`
  - 这些根目录会直接出现在 Streamlit 的 `Workspace root` 下拉框里
- `IRON_AGENT_DEFAULT_WORKSPACE_ROOT_ID`
  - 用于指定哪个根目录是默认选项
  - 保持 `default` 即可，除非你明确要切换默认公共工作空间

当前全 Docker 控制面的推荐约束：

- **优先把额外工作空间根目录建在 `IRON_AGENT_STACK_ROOT` 之下**
- 例如：
  - `/path/to/ironagent/data/workspaces-team-a`
  - `/path/to/ironagent/data/workspaces-team-b`

原因是当前 Compose 默认只把 `IRON_AGENT_STACK_ROOT` 以“同路径”挂进控制面容器。
如果你把额外根目录放在仓库外，例如 `/mnt/shared/team-a`，那么还需要自己把这个绝对路径额外挂载进 `security-service` 和 `admin` 容器；否则 Docker 控制面虽然能看到配置项，但无法在容器内访问这个目录。

常见示例：

macOS：

```env
IRON_AGENT_STACK_ROOT=/Users/yourname/work/ironagent
IRON_AGENT_RUNTIME_SOURCE_ROOT=/Users/yourname/work/ironagent-main
IRON_AGENT_HOST_IRON_AGENT_ROOT=/Users/yourname/.ironagent
```

Linux：

```env
IRON_AGENT_STACK_ROOT=/home/yourname/work/ironagent
IRON_AGENT_RUNTIME_SOURCE_ROOT=/home/yourname/work/ironagent-main
IRON_AGENT_HOST_IRON_AGENT_ROOT=/home/yourname/.ironagent
```

WSL2：

```env
IRON_AGENT_STACK_ROOT=/home/yourname/work/ironagent
IRON_AGENT_RUNTIME_SOURCE_ROOT=/home/yourname/work/ironagent-main
IRON_AGENT_HOST_IRON_AGENT_ROOT=/home/yourname/.ironagent
```

## 9. 启动全 Docker 控制面

在 IronAgent 仓库根目录执行：

```bash
cd /path/to/ironagent
docker compose up --build -d
```

查看服务状态：

```bash
docker compose ps
```

期望看到：

- `security-service` 为 `running`
- `admin` 为 `running`

如果需要查看日志：

```bash
docker compose logs -f security-service
docker compose logs -f admin
```

数据库迁移说明：

- `admin` 镜像入口默认会先执行数据库迁移再启动服务（`IRON_AGENT_AUTO_MIGRATE=1` 是默认值），避免“新镜像 + 旧 schema”导致的 `workspace_files` 缺表类故障。
- 只有当你要把迁移作为单独受控步骤执行时，才设置 `IRON_AGENT_AUTO_MIGRATE=0`。
- `security-service` 镜像虽然也打包了 `migrations/`（便于诊断），但不会自动执行迁移，避免两个服务在 schema 变更上竞态。
- 如果改了 schema 后 `admin` 起不来，先看 `admin` 容器日志；迁移报错是硬性部署失败，不是浏览器或 UI 问题。

## 10. 验证控制面服务

### 10.1 IronAgent 后端

```bash
curl http://127.0.0.1:8000/health
```

期望返回：

```json
{"status":"ok"}
```

### 10.2 Streamlit 后台

浏览器打开：

```text
http://127.0.0.1:8501
```

如果你改了 `.env` 里的端口，就用对应端口。

## 11. 在后台确认模型认证入口

进入 `Assistants` 页面后，你应该能看到这些字段：

- `Workspace root`
- `Workspace directory name`
- `Auto place outputs`
- `Primary model`
- `Authentication source`
- `Provider ID`
- `Provider base URL`
- `Provider API Key`

当前支持两种主线：

### 11.1 宿主机 IronAgent 认证

适用场景：

- 你已经在宿主机上登录过 IronAgent
- 想复用宿主机已有的 ChatGPT / Codex 登录态

推荐组合：

- `Primary model = openai-codex/gpt-5.1`
- `Authentication source = host_iron_agent`

### 11.2 Provider API Key

适用场景：

- 你要直连某个 provider
- 你要接本地 Ollama
- 你不想依赖宿主机 IronAgent 登录态

本地 Ollama 默认推荐组合：

- `Primary model = ollama/qwen2.5:1.5b`
- `Authentication source = provider_api_key`
- `Provider ID = ollama`
- `Provider base URL = http://host.docker.internal:11434`
- `Provider API Key = ollama-local`

如果你的机器资源充足，后续也可以切到：

- `Primary model = ollama/gpt-oss:20b`

`Auto place outputs` 建议保持开启。当前它已经会对 `write`、`edit`、`apply_patch` 生效：

- 类似 `report.docx`、`summary.xlsx` 这类正式产物，默认会落到 `persistent/`
- 类似 `smoke-output.json`、`draft.log` 这类临时产物，默认会落到 `temp/`
- 如果你显式写成 `temp/...` 或 `persistent/...`，系统不会改写
- `USER.md`、`AGENTS.md`、`.env` 这类敏感文件不会被“自动改道”，仍按安全规则直接保护

这套自动落点逻辑已经在 **Docker 控制面** 下重新做过 fresh live 复验：

- 未显式指定目录的 `final-report.docx` 会落到 `persistent/`
- 未显式指定目录的 `scratch-output.log` 会落到 `temp/`
- `apply_patch` 新增的 `debug-output.log` 会落到 `temp/`

## 12. 创建两个推荐验收助手

为了把两条主线都验掉，推荐创建两个助手。

### 12.1 助手 A：宿主机认证

建议填写：

- `Assistant ID`：`host-auth-demo`
- `Display name`：`Host Auth Demo`
- `Capability profile`：`dangerous`
- `Workspace root`：默认工作空间
- `Workspace directory name`：`host-auth-demo`
- `Auto place outputs`：保持默认开启
- `Primary model`：`openai-codex/gpt-5.1`
- `Authentication source`：`host_iron_agent`

点击 `Save assistant`。

期望结果：

- `runtime_status = running`
- 出现 `gateway_url`
- 出现 `gateway_token`

### 12.2 助手 B：Provider API Key / 本地 Ollama

建议填写：

- `Assistant ID`：`ollama-api-demo`
- `Display name`：`Ollama API Demo`
- `Capability profile`：`dangerous`
- `Workspace root`：默认工作空间
- `Workspace directory name`：`ollama-api-demo`
- `Auto place outputs`：保持默认开启
- `Primary model`：`ollama/qwen2.5:1.5b`
- `Authentication source`：`provider_api_key`
- `Provider ID`：`ollama`
- `Provider base URL`：`http://host.docker.internal:11434`
- `Provider API Key`：`ollama-local`

点击 `Save assistant`。

期望结果：

- `runtime_status = running`
- 出现 `gateway_url`
- 出现 `gateway_token`

## 13. 验证最小聊天连通性

### 13.1 宿主机认证助手

打开它的 `gateway_url`，输入 `gateway_token`，然后新开一个聊天，发送：

```text
Reply with exactly HOST_AUTH_UI_OK and nothing else.
```

期望结果：

```text
HOST_AUTH_UI_OK
```

补充说明：

- 如果这台机器开了全局代理，浏览器访问 `gateway_url` 前先确认 `127.0.0.1` 和 `localhost` 已加入直连白名单
- 否则像 `http://127.0.0.1:55202` 这种助手本地网关地址，可能会被代理错误接管，表现为假性 `502`
- 如果你想先在终端验证连通性，再打开浏览器，可先执行：

```bash
curl --noproxy '*' -I http://127.0.0.1:<gateway_port>
```

### 13.2 Provider API Key 助手

打开它的 `gateway_url`，输入 `gateway_token`，然后新开一个聊天，发送：

```text
Reply with exactly OLLAMA_UI_OK and nothing else.
```

期望结果：

```text
OLLAMA_UI_OK
```

## 14. 验证共享工作空间

宿主机上的公共工作空间根目录默认是：

```text
/path/to/ironagent/data/workspaces
```

每个助手各有一个子目录，例如：

```text
/path/to/ironagent/data/workspaces/host-auth-demo
/path/to/ironagent/data/workspaces/ollama-api-demo
```

容器内统一挂载路径是：

```text
/home/node/.ironagent/workspace
```

如果你在 `Assistants` 页面里给某个助手选择了“非默认”的公共工作空间根目录，那么它会落到：

```text
<你选择的工作空间根目录>/<Workspace directory name>
```

例如：

```text
/path/to/ironagent/data/workspaces-team-a/team-a-live-root
```

### 14.1 宿主机写，容器读

先在宿主机写一个文件：

```bash
echo "host persistent smoke" > /path/to/ironagent/data/workspaces/ollama-api-demo/persistent/host-smoke.txt
```

然后进入对应助手容器：

```bash
docker exec -it ironagent-assistant-ollama-api-demo sh
cat /home/node/.ironagent/workspace/persistent/host-smoke.txt
```

期望看到：

```text
host persistent smoke
```

### 14.2 容器写，宿主机读

仍在容器里执行：

```bash
echo "container temp smoke" > /home/node/.ironagent/workspace/temp/container-smoke.txt
```

回到宿主机读取：

```bash
cat /path/to/ironagent/data/workspaces/ollama-api-demo/temp/container-smoke.txt
```

期望看到：

```text
container temp smoke
```

### 14.3 验证“用户自选工作空间根目录”

如果你已经在 `.env` 中配置了 `IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON`，可以再创建一个 fresh 助手专门验证这条链路。

建议填写：

- `Assistant ID`：`custom-root-demo`
- `Display name`：`Custom Root Demo`
- `Capability profile`：`dangerous`
- `Workspace root`：选择 `团队A工作空间`
- `Workspace directory name`：`team-a-live-root`
- `Auto place outputs`：保持开启
- `Primary model`：`openai-codex/gpt-5.1`
- `Authentication source`：`host_iron_agent`

点击 `Save assistant` 后，期望：

- `runtime_status = running`
- `workspace_root` 显示为：

```text
/path/to/ironagent/data/workspaces-team-a/team-a-live-root
```

然后进入该助手，做三条最小 live 验证：

1. 写正式结果文件：

```text
Create a file named team-report.docx with the exact text custom root persistent smoke. Do not specify temp/ or persistent/.
```

期望文件出现在：

```text
/path/to/ironagent/data/workspaces-team-a/team-a-live-root/persistent/team-report.docx
```

2. 写临时日志文件：

```text
Create a file named session-smoke.log with the exact text custom root temp smoke. Do not specify temp/ or persistent/.
```

期望文件出现在：

```text
/path/to/ironagent/data/workspaces-team-a/team-a-live-root/temp/session-smoke.log
```

3. 用 `apply_patch` 新增临时文件：

```text
Add a new file named draft-session.log with the exact text custom root patch temp smoke. Do not specify temp/ or persistent/.
```

期望文件出现在：

```text
/path/to/ironagent/data/workspaces-team-a/team-a-live-root/temp/draft-session.log
```

本轮实测已经确认这三条在 Docker 控制面下都通过。

## 15. 验证审批与拦截

这一段建议使用 `dangerous` profile 的助手。

### 15.1 删除 `persistent/` 文件应进入用户审批

先准备文件：

```bash
echo "delete me" > /path/to/ironagent/data/workspaces/ollama-api-demo/persistent/delete-me.txt
```

然后让 IronAgent 执行删除：

```text
Delete ./persistent/delete-me.txt and reply with exactly DONE_DELETE_PERSISTENT when finished.
```

期望结果：

- IronAgent 后台出现一条 `user` 审批
- 你点 `Allow` 后，助手继续执行
- 最终回复：

```text
DONE_DELETE_PERSISTENT
```

### 15.2 调外部 API 应进入管理员审批

在聊天里发送：

```text
Run curl -I https://example.com and reply with exactly DONE_EXTERNAL_API when finished.
```

期望结果：

- IronAgent 后台出现一条 `admin` 审批
- 你点 `Allow` 后，助手继续执行
- 最终回复：

```text
DONE_EXTERNAL_API
```

### 15.3 覆盖敏感文件应直接拒绝

在聊天里发送：

```text
Overwrite AGENTS.md with the single line hacked and reply with exactly DONE_OVERWRITE_AGENTS when finished.
```

期望结果：

- 不进入审批
- 工具被直接拒绝
- `AGENTS.md` 内容保持不变

## 16. 验证 `office-docs`

当前默认配置下，fresh 助手在创建时会自动把 `office-docs` 预置到工作区 `skills/`。

如果你怀疑自动预置没有生效，可以先检查：

```bash
ls -la /path/to/ironagent/data/workspaces/host-auth-demo/skills
```

确认存在 `office-docs` 后，再让助手使用它。

目标是生成这 4 个文件：

- `agent-word.docx`
- `agent-word.pdf`
- `agent-sheet.xlsx`
- `agent-sheet.pdf`

期望最终结果：

- Word 生成成功
- Excel 生成成功
- 两个 PDF 都生成成功

如果你只关心离线办公主线，这一步做完就说明 Word / Excel / PDF 生成链路已经打通。

## 17. 验证 `local-pdf`

当前默认配置下，fresh 助手在创建时会自动把 `local-pdf` 预置到工作区 `skills/`。

如果你怀疑自动预置没有生效，可以先检查：

```bash
ls -la /path/to/ironagent/data/workspaces/host-auth-demo/skills
```

确认存在 `local-pdf` 后，再让助手对已有 PDF 做本地处理。

这条 skill 不依赖外部模型，适合做离线 PDF 连通性验证。

期望生成：

- `extracted.txt`
- `verified-copy.pdf`
- `summary.pdf`

如果这一步成功，就说明 PDF 的本地离线处理链路也已经打通。

当前说明：

- 在默认全 Docker 配置里，`local-pdf` 会和 `office-docs` 一起被自动预置到 fresh 助手工作区。
- 如果你这一步没有复现成功，优先检查：
  - `.env` 里的 `IRON_AGENT_RUNTIME_SOURCE_ROOT` 是否正确
  - `docker compose up --build -d` 后控制面容器是否已加载新配置
  - 助手是否是在新配置生效后 fresh 创建的

## 18. 关闭与清理

如果你暂时不再使用这套全 Docker 控制面，可以在 `ironagent` 仓库根目录执行：

```bash
docker compose down
```

如果你还想顺手清理掉不再使用的 fresh 助手容器：

```bash
docker ps -a --format '{{.Names}}' | grep '^ironagent-assistant-' | xargs -r docker rm -f
```

注意：

- 上面第二条会删除所有 `ironagent-assistant-*` 容器
- 如果你还想保留某些助手，请不要直接执行这条

## 19. 常见问题

### 19.1 后台能打开，但创建助手失败

先看：

```bash
docker compose logs -f security-service
docker compose logs -f admin
```

常见原因：

- `IRON_AGENT_STACK_ROOT` 不是绝对路径
- `IRON_AGENT_STACK_ROOT` 在容器内没有按同一路径挂载
- `config/` 或 `data/` 缺失
- Docker socket 没挂进去

### 19.2 Windows 原生 PowerShell 下路径不对

这是预期风险。  
全 Docker 默认方案在 Windows 上推荐改为 **WSL2**，不要把原生 `C:\...` 路径当主线。

### 19.3 Ollama 容器内访问不到

先确认宿主机可用：

```bash
curl http://127.0.0.1:11434/api/tags
```

再确认助手配置里填的是：

```text
http://host.docker.internal:11434
```

不要填 `127.0.0.1:11434`，因为那会指向助手容器自己，不是宿主机。

## 20. 一句话总结

如果你想在另一台设备上完整复现当前项目，最稳的方式就是：

1. 在宿主机或 WSL2 里准备 `ironagent` 和 `ironagent-main`
2. 构建 `ironagent-runtime:dev`
3. 配好 `.env`
4. 执行 `docker compose up --build -d`
5. 进入 Streamlit `Assistants` 页面
6. 用 `host_iron_agent` 和 `provider_api_key` 两条模型接入各建一个 fresh 助手
7. 依次验收聊天、工作空间、审批、`office-docs`、`local-pdf`
