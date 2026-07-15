# IronAgent 小白版 Docker 部署到使用手册

这份文档只讲一件事：

从零把 IronAgent 跑起来，然后创建一个助手，拿到 `gateway url` 和 `gateway token`，最后把一个 skill 和一个 plugin 装进这个助手。

如果你不想看很长的技术文档，直接照这份做就行。

## 1. 你需要准备什么

准备好下面这几样：

1. Docker Desktop
2. `ironagent` 仓库
3. `ironagent-main` 仓库
4. 宿主机的 `~/.ironagent`
   - 如果你要走 `host_iron_agent` 认证，这个目录里要有已认证过的 IronAgent 配置

推荐目录：

```text
/path/to/ironagent
/path/to/ironagent-main
```

Windows 用户建议：

1. 用 Docker Desktop + WSL2
2. 把两个仓库都放进 WSL2 文件系统
3. 后面的命令都在 WSL2 终端执行

## 2. 第一步：安装依赖

在 `ironagent` 仓库执行：

```bash
cd /path/to/ironagent
uv sync
```

在 `ironagent-main` 仓库执行：

```bash
cd /path/to/ironagent-main
pnpm install
```

## 3. 第二步：构建 IronAgent 助手镜像

回到 `ironagent` 仓库：

```bash
cd /path/to/ironagent
make ironagent-image IRON_AGENT_REPO_PATH=/path/to/ironagent-main
```

检查镜像是否存在：

```bash
docker images | grep ironagent-runtime
```

看到 `ironagent-runtime:dev` 就说明成功了。

## 4. 第三步：准备 `.env`

复制模板：

```bash
cd /path/to/ironagent
cp .env.example .env
```

最少要改这几项：

```env
IRON_AGENT_STACK_ROOT=/path/to/ironagent
IRON_AGENT_RUNTIME_SOURCE_ROOT=/path/to/ironagent-main
IRON_AGENT_HOST_IRON_AGENT_ROOT=/path/to/your/.ironagent
IRON_AGENT_SECURITY_SERVICE_PORT=19000
IRON_AGENT_ADMIN_PORT=19502
IRON_AGENT_RUNTIME_HOOK_SERVICE_URL=http://host.docker.internal:19000
IRON_AGENT_RUNTIME_CONTAINER_EXTRA_HOST=host.docker.internal:host-gateway
IRON_AGENT_RUNTIME_SEED_WORKSPACE_SKILLS=office-docs,local-pdf
```

说明：

1. `IRON_AGENT_STACK_ROOT`
   - 必须是 `ironagent` 仓库绝对路径
2. `IRON_AGENT_RUNTIME_SOURCE_ROOT`
   - 必须是 `ironagent-main` 仓库绝对路径
3. `IRON_AGENT_HOST_IRON_AGENT_ROOT`
   - 必须是宿主机 `.ironagent` 绝对路径
4. `19000/19502`
   - 这是推荐宿主机端口

如果宿主机必须依赖代理或 VPN 才能访问外部模型，还要继续补这几项：

```env
IRON_AGENT_RUNTIME_HTTP_PROXY=http://host.docker.internal:7890
IRON_AGENT_RUNTIME_HTTPS_PROXY=http://host.docker.internal:7890
IRON_AGENT_RUNTIME_NO_PROXY=localhost,127.0.0.1,host.docker.internal
```

说明：

1. 如果你本机开的是 Clash、v2rayN、公司代理等本地代理
   - 不要写 `127.0.0.1:7890`
   - 要写 `host.docker.internal:7890`
2. 这是为了让助手容器也能通过宿主机代理访问模型服务
3. `NO_PROXY` 至少建议包含：
   - `localhost`
   - `127.0.0.1`
   - `host.docker.internal`
   - 否则容器访问本地 Hook 服务时可能被错误转发到代理

## 5. 第四步：启动控制面

执行：

```bash
cd /path/to/ironagent
docker compose up -d --build security-service admin
```

检查是否成功：

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl http://127.0.0.1:19000/health
curl -I http://127.0.0.1:19502
```

期望结果：

1. 有 `ironagent-security-service-1`
2. 有 `ironagent-admin-1`
3. `health` 返回 `{"status":"ok"}`
4. `19502` 返回 `HTTP/1.1 200 OK`

如果你改了代理相关的 `.env`，也要重新执行这条命令，让控制面把新代理配置带进后续新建的助手容器。

## 6. 第五步：打开后台

浏览器打开：

- `http://127.0.0.1:19502`

你会看到 `IronAgent Admin` 页面。

## 7. 第六步：创建助手

进入后台的 `Assistants` 页面，创建一个新助手。

推荐填写：

1. `assistant_id`
   - 例如：`demo-docker-01`
2. `name`
   - 例如：`Demo Docker 01`
3. `capability_profile`
   - 先选 `onlyread`
4. 模型方式
   - 如果你已经在宿主机 IronAgent 上完成认证，先选 `host_iron_agent`

补充说明：

1. `host_iron_agent`
   - 适合已经在宿主机 `~/.ironagent` 完成认证的场景
   - 当前优先用于 `openai-codex/*` 这类模型
2. `provider_api_key`
   - 适合 `modelstudio / zai / ollama / OpenAI-compatible API` 这类按地址和密钥接入的模型
   - 这时应在页面里继续填写：
     - `Primary model`
     - `Provider ID`
     - `Provider base URL`
     - `Provider API Key`
3. 如果你打算在其他机器上接 `qwen / glm / kimi / minimax / ollama`，通常应选 `provider_api_key`

保存后，系统会自动：

1. 创建工作空间
2. 创建运行时状态目录
3. 启动一个独立的 IronAgent 容器
4. 生成 `gateway url`
5. 生成 `gateway token`

## 8. 第七步：拿到 Gateway 信息

保存成功后，你需要记住这两个值：

1. `Gateway URL`
2. `Gateway Token`

你也可以在宿主机文件里看到它们：

```bash
cat /path/to/ironagent/data/assistants/assistants.json
```

或者直接看某个助手的运行时配置：

```bash
cat /path/to/ironagent/data/runtimes/<assistant_id>/state/ironagent.json
```

## 9. 第八步：打开这个助手

直接用浏览器打开：

- `Gateway URL`

例如：

- `http://127.0.0.1:55202`

这个页面就是该助手的 IronAgent Control 入口。

然后输入：

1. WebSocket URL
   - 一般就是把 `http://` 改成 `ws://`
   - 例如：`http://127.0.0.1:56610` 对应 `ws://127.0.0.1:56610`
2. Gateway Token
   - 使用刚才拿到的 token

注意：

- 如果宿主机开了全局代理，浏览器打开 `gateway_url` 时也要保证 `127.0.0.1` 和 `localhost` 走直连
- 否则本地随机端口形式的助手网关，例如 `http://127.0.0.1:56610`，可能会被错误转发到代理端口，表现成假性 `502`
- 如果你要用命令行验证 `gateway_url`，优先使用不走代理的方式，例如：

```bash
curl --noproxy '*' -I http://127.0.0.1:<gateway_port>
```

## 10. 第九步：确认工作空间已创建

执行：

```bash
find /path/to/ironagent/data/workspaces/<assistant_id> -maxdepth 2 -type d | sort
```

你应该看到：

1. `temp/`
2. `persistent/`
3. `skills/`
4. `plugins/`

默认还会自动预置：

1. `skills/office-docs`
2. `skills/local-pdf`

## 11. 第十步：上传一个 skill 和一个 plugin

进入后台的 `Market` 页面。

### 11.1 上传 skill

准备一个 zip 包，里面至少要有：

1. `manifest.json`
2. `SKILL.md`

上传后，页面会出现一条新的 skill 记录。

### 11.2 上传 plugin

准备一个 zip 包，里面至少要有：

1. `manifest.json`
2. 插件主文件，例如 `plugin.py`

上传后，页面会出现一条新的 plugin 记录。

## 12. 第十一步：审核并安装

在 `Market` 页面分别选中：

1. 你刚上传的 skill
2. 你刚上传的 plugin

然后执行：

1. 先点一次 `Download archive`
2. 确认浏览器能成功下载归档包
3. 再点 `Approve`
4. 再点 `Install to assistant`

安装目标选择你刚创建的助手。

## 13. 第十二步：检查安装结果

执行：

```bash
find /path/to/ironagent/data/workspaces/<assistant_id>/skills -maxdepth 3 -type f | sort
find /path/to/ironagent/data/workspaces/<assistant_id>/plugins -maxdepth 3 -type f | sort
find /path/to/ironagent/data/market -maxdepth 4 -type f | sort
```

期望结果：

1. skill 实际落到：
   - `data/workspaces/<assistant_id>/skills/<skill_slug>/`
2. plugin 实际落到：
   - `data/workspaces/<assistant_id>/plugins/<plugin_slug>/`
3. 市场包和目录索引实际落到：
   - `data/market/catalog.json`
   - `data/market/packages/...`

如果你想继续看更细的市场操作说明，例如：

1. `Download archive` 到底下载到哪里
2. `Install to assistant` 为什么会和某个助手对应上
3. 助手怎么查看自己的已装 `skills/plugins`
4. 助手怎么停用、启用、复用这些包
5. 容器里实际看到的是哪个目录
6. 怎么做 office skill/plugin 的最小验证

请继续看：

- `docs/market-skill-plugin-operation.zh-CN.md`

## 14. 一套最小成功标准

如果你只想确认“这套系统能用了”，至少做到下面这些：

1. `docker compose up -d --build security-service admin`
2. 能打开 `http://127.0.0.1:19502`
3. 新建助手后自动生成容器
4. 能拿到 `gateway url` 和 `gateway token`
5. 能打开该 `gateway url`
6. `Market` 页面上传一个 skill 成功
7. 这个 skill 能通过 `Install to assistant` 安装到该助手的 `skills/` 目录
8. 在 `Assistants` 页面能看到这个 skill 已装

做到这里，就说明“部署到使用”主链路已经打通。

## 15. 部署通过后，继续做功能与安全验收

这份手册解决的是“从零部署到可以使用”的主链路。

如果你还要继续验收下面这些能力：

1. 敏感文件写入拦截
2. 容器内调用外部 API 指令拦截
3. `persistent/` 删除走用户审批
4. 第三方命令和插件安装走管理员审批
5. 审批记录与审计日志

请继续执行：

- `docs/functional-test.zh-CN.md`

重点看：

1. `4.10 安全 Hook 冒烟`
2. `4.11 安全拦截与审批链路验证`

## 16. 最容易踩的坑

### 16.1 本机开了全局代理

如果你开了 Clash、Surge、V2Ray、代理软件，最容易出现这个问题：

- 访问本地地址，例如 `127.0.0.1:19000`
- 或者访问助手自己的 `gateway_url`
- 却被转发到本地代理端口，例如 `127.0.0.1:7890`
- 最终返回 `502`

处理方法：

1. 给下面两个地址加直连：
   - `127.0.0.1`
   - `localhost`
2. 如果你用 Python 脚本请求本地接口，显式关闭环境代理：

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

3. 如果你直接检查助手网关，优先这样验证：

```bash
curl --noproxy '*' -I http://127.0.0.1:<gateway_port>
```

### 16.2 Docker 命令卡住

如果 `docker ps`、`docker compose ps`、`docker images` 卡住：

1. 先重启 Docker Desktop
2. 再重新执行：

```bash
docker compose up -d --build security-service admin
```

### 16.3 Gateway 容器刚起来时显示 `starting`

这是正常现象。

只要你看到：

1. 助手容器已经存在
2. `gateway_url` 已生成
3. 容器日志出现 `listening on ws://0.0.0.0:18789`

通常就说明它已经开始工作了，稍等几秒后 health 会变成 `healthy`。
