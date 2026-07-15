# IronAgent 权限管理系统文档

## 概述

IronAgent 是 IronAgent 的企业安全控制层，提供统一的**策略决策点（Policy Decision Point, PDP）** 架构。所有请求必须收集齐所有上下文，经过统一的矩阵评估后才能得出结论，**绝不允许中途随意"短路放行"**。

当前默认审批模式为 `IRON_AGENT_APPROVAL_MODE=disabled`。PDP 仍完整执行 P0-P3 评估，P0 硬拒绝仍然不可绕过；原本会返回 `pending_approval` 的 P1/P2/P3 分支会在最终响应阶段转换为 `allow`，并写入 `approval.bypassed` 审计事件。需要恢复人工审批链路时，可显式设置 `IRON_AGENT_APPROVAL_MODE=enabled`。

---

## 1. 架构设计

### 1.1 决策流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Pre-Execute Hook Request                       │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    1. 收集决策上下文 (DecisionContext)                │
│   - Subject: Profile 类型（chat/onlyread/onlywrite/dangerous）       │
│   - Action: 动作类型（Read/Write/Delete/Execute/Network/Install）   │
│   - Resource: 目标路径类型                                           │
│   - Risk: 综合风险等级（Low/Medium/High/Critical）                   │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    2. 按优先级评估（绝对不允许短路）                   │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ P0: 系统级黑名单 ──────────────────────────────────────────  │   │
│   │     → Secret 路径、敏感文件写入/删除                          │   │
│   │     → 直接 Deny，不可绕过                                     │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                    │                                │
│                                    ▼                                │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ P1: 风险等级强制拦截 ───────────────────────────────────────  │   │
│   │     → Critical/High 风险等级                                 │   │
│   │     → 第三方命令、包管理器安装                                 │   │
│   │     → admin 审批                                              │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                    │                                │
│                                    ▼                                │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ P2: 核心资产保护 ──────────────────────────────────────────  │   │
│   │     → Persistent 目录 Delete/Write → user 审批              │   │
│   │     → Read/Create (without overwrite) → 继续评估             │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                    │                                │
│                                    ▼                                │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ P3: Profile 授权白名单 ────────────────────────────────────  │   │
│   │     → allowed_tools → allow                                 │   │
│   │     → approval_tools → user 审批                             │   │
│   │     → 都不在 → deny                                          │   │
│   └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         决策结果                                     │
│   - allow: 允许执行                                                  │
│   - deny: 拒绝执行                                                   │
│   - pending_approval: 等待审批（user/admin）                         │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心组件

| 组件 | 文件路径 | 职责 |
|------|----------|------|
| SecurityDecisionEngine | `src/security_service/decision.py` | 主决策引擎，协调整个决策流程 |
| request_policy | `src/security/request_policy.py` | P0-P2 策略评估（系统黑名单、风险等级、资产保护） |
| capability | `src/security/capability.py` | Capability Profile 数据模型 |
| whitelist | `src/security/whitelist.py` | Profile 白名单辅助函数 |
| pruning | `src/security/pruning.py` | 永久裁剪工具加载 |

---

## 2. 决策上下文 (DecisionContext)

### 2.1 四维属性

```python
@dataclass(slots=True)
class DecisionContext:
    """统一决策上下文，收集四维属性"""
    tool_name: str           # 工具名称
    profile_name: str        # Profile 类型
    action: ActionClass | None  # 动作类型
    path_assessments: list[PathAssessment]  # 路径分类结果
    risk_level: RiskLevel    # 风险等级
    command: str | None = None  # 原始命令（用于命令工具）
    metadata: dict = field(default_factory=dict)
```

### 2.2 ActionClass（动作类型）

```python
ActionClass = Literal["read", "write", "delete", "execute", "network", "install"]
```

| 动作 | 工具示例 | 说明 |
|------|----------|------|
| read | `read` | 文件读取 |
| write | `write`, `edit`, `apply_patch` | 文件写入/编辑 |
| delete | `delete`, `remove`, `unlink` | 文件删除 |
| execute | `exec`, `process`, `code_execution` | 命令执行 |
| network | `curl`, `wget` | 网络请求 |
| install | `npm`, `pip`, `yarn` | 包安装 |

### 2.3 PathClass（路径类型）

```python
PathClass = Literal[
    "workspace_root",      # 工作区根目录
    "workspace_temp",      # 临时目录 (temp/)
    "workspace_persistent", # 持久化目录 (persistent/)
    "workspace_sensitive", # 敏感文件 (.env, USER.md, AGENTS.md)
    "container_sensitive", # 容器敏感路径 (/etc/, /usr/local/bin/, /app/)
    "container_secret",     # Secret 路径 (/root/.ssh/)
    "other",               # 其他路径
]
```

---

## 3. 优先级体系

### 3.1 优先级定义

```python
class Priority(IntEnum):
    """策略优先级，数值越小优先级越高"""
    P0_BLACKLIST = 0          # 系统级黑名单
    P1_RISK_LEVEL = 1         # 风险等级拦截
    P2_ASSET_PROTECTION = 2   # 核心资产保护
    P3_PROFILE_WHITELIST = 3  # Profile 授权白名单
```

### 3.2 P0: 系统级绝对黑名单

**特点**：直接 Deny，不可绕过。无论 Profile 是什么，无论风险等级高低，一律拒绝。

#### 触发条件

| 场景 | 示例 | 决策 | 原因 |
|------|------|------|------|
| Secret 路径访问 | `/root/.ssh/`, `/root/.ssh/id_rsa` | Deny | secret path is not accessible |
| 工作区敏感文件写入/删除 | `.env`, `USER.md`, `AGENTS.md` | Deny | sensitive path is read-only |
| 容器敏感路径写入/删除 | `/etc/hosts`, `/usr/local/bin/...` | Deny | sensitive path is read-only |

#### 路径分类规则

```python
# Secret 路径前缀
CONTAINER_SECRET_PREFIXES = ("/root/.ssh/",)

# 容器敏感路径前缀
CONTAINER_SENSITIVE_PREFIXES = ("/etc/", "/usr/local/bin/", "/app/")

# 工作区敏感文件名
SENSITIVE_WORKSPACE_BASENAMES = {".env", "USER.md", "AGENTS.md"}
```

#### 注意事项

- 路径会经过**词法规范化**（如 `/etc/../etc/hosts` → `/etc/hosts`）
- P0 规则**只拦截写入/删除操作**，读取敏感路径是允许的（除非在 P1 阶段被拦截）

---

### 3.3 P1: 风险等级强制拦截

**特点**：无论 Profile 授权如何，高风险操作必须经过 admin 审批。

#### 触发条件

| 条件 | 触发规则 | 审批级别 |
|------|----------|----------|
| 风险等级 = Critical | 直接触发 | admin |
| 风险等级 = High | 直接触发 | admin |
| 包含网络命令 | `curl`, `wget` | admin |
| 包含包管理器安装 | `npm`, `pip`, `yarn`, `pnpm`, `uv`, `poetry` | admin |
| 包含第三方命令 | `docker`, `aws`, `aliyun`, `gsutil` | admin |
| 包含插件安装命令 | `ironagent plugins add ...` | admin |

#### 高风险命令检测

```python
# 第三方命令
THIRD_PARTY_COMMANDS = {
    "aliyun", "aws", "curl", "docker", "gsutil",
    "npm", "npx", "obsutil", "pip", "pip3",
    "pnpm", "poetry", "uv", "wget", "yarn",
}

# 安装命令
INSTALL_COMMANDS = {"npm", "pip", "pip3", "pnpm", "yarn", "uv", "poetry", "gem"}

# 网络命令
NETWORK_COMMANDS = {"curl", "wget"}

# 插件安装命令
PLUGIN_INSTALL_COMMANDS = {
    ("ironagent", "plugins", "add"),
    ("ironagent", "plugin", "add"),
}
```

#### 命令解析能力

- 支持命令链：`cd /workspace && rm persistent/file.txt`
- 支持 shell 包装：`bash -c "rm persistent/file.txt"`
- 支持嵌套命令：`sh -lc "npm install && curl ..."`

---

### 3.4 P2: 核心资产保护

**特点**：Persistent 目录作为受保护的资源卷，基于动作进行统一拦截。

#### 文件 API 与命令工具的不同处理

由于**命令工具无法在 pre-execution 阶段区分创建和覆盖**（文件系统状态未知），所以采用不同的策略：

| 场景 | 目标 | 操作类型 | 决策 | 审批级别 | 说明 |
|------|------|----------|------|----------|------|
| **文件 API** | `persistent/` | Create (首次) | P2 放行 → 直接允许 | - | - |
| **文件 API** | `persistent/` | Overwrite | pending_approval | user | 需要审批 |
| **文件 API** | `persistent/` | Delete | pending_approval | user | 需要审批 |
| **文件 API** | `persistent/` | Read | P2 放行 → 直接允许 | - | - |
| **命令工具** | `persistent/` | Create/Write | pending_approval | user | **保守策略**：无法区分创建和覆盖，都需要审批 |
| **命令工具** | `persistent/` | Delete | pending_approval | user | 需要审批 |
| **命令工具** | `persistent/` | Read | P2 放行 → P3 决定 | - | P3 允许（read 在 allowed_tools） |

#### 实际动作检测

对于命令工具（如 `exec`），需要从命令中检测实际操作：

```python
def _detect_file_action(context: DecisionContext) -> ActionClass | None:
    """检测命令中的实际文件操作"""
    # 检查删除命令
    if _extract_delete_targets(tokens):
        return "delete"

    # 检查写入命令 (cp, mv, install, tee, dd)
    if _extract_write_targets(tokens):
        return "write"

    # 检查重定向操作 (>, >>)
    if _has_redirect_to_persistent(command):
        return "write"
```

#### 支持的命令检测

| 命令类型 | 命令示例 | 检测操作 | P2 处理 |
|----------|----------|----------|---------|
| 删除命令 | `rm`, `unlink`, `del`, `remove-item` | delete | user 审批 |
| 写入命令 | `cp`, `mv`, `install`, `tee`, `dd` | write | user 审批 |
| 重定向 | `> persistent/file`, `>> persistent/log` | write | user 审批 |

---

### 3.5 P3: Profile 授权白名单

**特点**：前置的高风险和资源保护都通过后，才轮到 Profile 发挥作用。**所有工具（包括命令工具）都需经过 P3 检查**。

#### 触发条件

| 条件 | 决策 | 审批级别 |
|------|------|----------|
| 工具在 `allowed_tools` 中 | allow | - |
| 工具在 `approval_tools` 中 | pending_approval | user |
| 都不在 | deny | - |

#### 统一 PDP 架构说明

在统一 PDP 架构下，**所有工具都必须经过完整的 P0 → P1 → P2 → P3 评估流程**：

```
┌────────────────────────────────────────────────────────────────────┐
│  命令工具 (exec) 完整评估流程示例                                     │
├────────────────────────────────────────────────────────────────────┤
│  1. P0 检查 → Secret/敏感路径？ → 否 → 继续                         │
│  2. P1 检查 → 第三方命令？ → 否 → 继续                              │
│  3. P2 检查 → 操作 persistent？ → 否 → 继续                         │
│  4. P3 检查 → exec 在 allowed_tools？ → 是 → allow                │
└────────────────────────────────────────────────────────────────────┘
```

这意味着 `dangerous` profile 下的白名单工具默认直接放行；只有 P0 hard-deny
路径或工具会被拒绝。

#### Profile 配置示例

```yaml
# config/capabilities/dangerous.yaml
name: dangerous
level: 3
allowed_tools:
  - read
  - write
  - edit
  - exec
  - process
  - cron
  - sessions_send
  - sessions_spawn
  - subagents
  - apply_patch
  - web_search
  - web_fetch
  - memory_get
  - memory_search
  - session_status
  - sessions_list
  - sessions_history
  - code_execution
approval_tools: []
```

```yaml
# config/capabilities/onlywrite.yaml
name: onlywrite
level: 2
allowed_tools:
  - read
  - write
  - edit
  - apply_patch
  - web_search
  - web_fetch
  - memory_get
  - memory_search
  - session_status
  - sessions_list
  - sessions_history
approval_tools: []
```

---

## 4. 永久裁剪工具 (Pruned Tools)

永久裁剪工具在 P0 之前被检查，属于**最高优先级的硬拦截**。

### 配置文件

`config/security/pruning.yaml`:

```yaml
tools:
  - system_reset
  - unsafe_exec
```

### 代码实现

```python
# src/security/pruning.py
def load_pruned_tools() -> set[str]:
    raw = load_yaml(pruning_file())
    return set(raw.get("tools", []))
```

### 决策逻辑

```python
# 在 SecurityDecisionEngine 中
pruned_tools = load_pruned_tools()
if tool_name in pruned_tools:
    return PreExecuteHookResponse(
        decision="deny",
        reason="tool is permanently pruned",
    )
```

---

## 5. Capability Profiles

### 5.1 Profile 类型

| Profile | Level | 说明 |
|---------|-------|------|
| chat | 1 | 基础对话权限，仅允许安全的读操作 |
| onlyread | 1 | 只读权限 |
| onlywrite | 2 | 可读写文件，但无 shell 访问 |
| dangerous | 3 | 高风险权限，可执行命令，需审批 |

### 5.2 Profile 加载

```python
# src/security/capability.py
@dataclass(slots=True)
class CapabilityProfile:
    name: str
    level: int
    allowed_tools: set[str]
    approval_tools: set[str]
    metadata: dict[str, Any]

def load_capability_profile(path: Path) -> CapabilityProfile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return CapabilityProfile(
        name=raw.get("name", path.stem),
        level=int(raw.get("level", 0)),
        allowed_tools=set(raw.get("allowed_tools", [])),
        approval_tools=set(raw.get("approval_tools", [])),
        metadata={...},
    )
```

### 5.3 Profile 优先级

**助手绑定的 Profile 优先于请求中的 Profile**：

```python
async def _resolve_profile_name(self, request: PreExecuteHookRequest) -> str:
    assistant = await self._load_assistant(request)
    if assistant:
        return assistant.capability_profile  # 助手 Profile 优先
    return request.assistant.capability_profile
```

---

## 6. 工作区文件自动放置

### 6.1 自动放置规则

| 文件名关键词 | 放置目标 | 说明 |
|--------------|----------|------|
| `report`, `output`, `result`, `artifact` | `persistent/` | 持久化文件 |
| `smoke`, `temp`, `test`, `tmp`, `cache` | `temp/` | 临时文件 |
| `.env`, `USER.md`, `AGENTS.md` | 保持原位 | 敏感文件不自动移动 |

### 6.2 决策时序

```
┌────────────────────────────────────────────────────────────┐
│  1. 原始请求: write { path: "report.pdf" }                   │
│                                                             │
│  2. 自动放置: persistent/report.pdf                         │
│     (基于文件名关键词)                                        │
│                                                             │
│  3. 放置后路径走 P2 检查 → user 审批                          │
└────────────────────────────────────────────────────────────┘
```

### 6.3 敏感文件保护

敏感文件**不会被自动放置**，而是直接被 P0 拦截：

```
请求: write { path: "AGENTS.md" }
        ↓
  自动放置尝试 → 但检测到是敏感文件
        ↓
  不执行放置，直接 Deny (P0)
```

---

## 7. 工作区文件 API 保护

### 7.1 统一 PDP P2 规则

工作区文件 API 操作与命令工具执行统一遵循 **PDP P2 核心资产保护规则**：

| 操作 | 目标 | 审批级别 | 代码位置 |
|------|------|----------|----------|
| 上传/覆盖 | `persistent/` | **user** | `file_upload_service.py` |
| 删除 | `persistent/` | **user** | `file_delete_service.py` |
| 上传/创建 | `temp/` | 无需审批 | - |
| 上传/创建 | `persistent/` (首次) | 无需审批 | - |

### 7.2 代码示例

**文件上传服务** (`file_upload_service.py`):
```python
if existing is not None and bucket == "persistent" and overwrite:
    # P2: 核心资产保护 - 覆盖 persistent 需要 user 审批
    approval = self.approval_service.create_request(
        "workspace_file_overwrite",
        assistant_id=resolved_id,
        approval_scope="user",  # 统一为 user 审批
        reason=f"Overwrite persistent file: {filename}",
        target=filename,
    )
```

**文件删除服务** (`file_delete_service.py`):
```python
if record.bucket == "persistent":
    approval = self.approval_service.create_request(
        "workspace_file_delete",
        approval_scope="user",
        reason=f"Delete persistent file: {record.filename}",
        target=record.filename,
    )
```

### 7.3 一致性保证

统一 PDP 架构确保：
- **命令工具** `exec rm persistent/file.txt` → user 审批
- **命令工具** `exec echo "x" > persistent/file.txt` → user 审批（无法区分创建/覆盖，保守策略）
- **文件 API** DELETE `persistent/file.txt` → user 审批
- **文件 API** 覆盖 `persistent/file.txt` → user 审批
- **文件 API** 首次上传 `persistent/` → 无需审批（可以检测文件是否存在）

### 7.4 设计说明

文件 API 与命令工具采用不同策略的原因：

| 场景 | 能否区分创建/覆盖 | 采用策略 |
|------|-------------------|----------|
| **文件 API** | ✅ 可以（`existing is not None`） | 首次创建允许，覆盖需要审批 |
| **命令工具** | ❌ 无法在 pre-execution 阶段判断 | 保守策略：写入 persistent 都审批 |

---

## 8. 审批系统

### 8.1 审批记录结构

```python
class ApprovalRecord:
    id: str                    # 审批 ID
    tool_name: str             # 工具名称
    assistant_id: str          # 助手 ID
    capability_profile: str    # Capability Profile
    approval_scope: Literal["user", "admin"]  # 审批级别
    reason: str                # 审批原因
    target: str | None         # 目标路径
    status: Literal["pending", "approved", "denied"]
    resolution: Literal["allow", "deny"] | None
    requested_at: datetime
    expires_at: datetime       # 默认 5 分钟
    resolved_at: datetime | None
```

### 8.2 审批级别

| 级别 | 说明 | 典型使用场景 |
|------|------|--------------|
| user | 普通用户审批 | Persistent 文件操作、Profile 定义的审批工具 |
| admin | 管理员审批 | 高风险操作（Critical/High）、第三方命令、插件安装 |

### 8.3 审批有效期

当前实现默认审批有效期为 **5 分钟**。

> ⚠️ 注意：当前后端没有"超时后自动拒绝并落地执行"的机制，过期时间更多是"记录和返回给调用方"。

---

## 9. 决策结果

### 9.1 返回类型

```python
class PreExecuteHookResponse:
    request_id: str
    decision: Literal["allow", "deny", "pending_approval"]
    reason: str | None
    capability_profile: str
    approval: ApprovalContext | None  # 仅 pending_approval 时有值
    metadata: dict[str, Any]
    tool_args: dict | None  # 处理后的工具参数（如自动放置后的路径）
```

### 9.2 决策对照表

| 场景 | `enabled` 决策 | 默认 `disabled` 决策 | 原因示例 |
|------|----------------|----------------------|----------|
| 永久裁剪工具 | deny | deny | tool is permanently pruned |
| Secret 路径访问 | deny | deny | secret path is not accessible |
| 敏感文件写入/删除 | deny | deny | sensitive path is read-only |
| Critical/High 风险 | pending_approval | allow + `approval.bypassed` 审计 | high-risk operation requires administrator approval |
| 第三方命令 | pending_approval | allow + `approval.bypassed` 审计 | third-party command requires administrator approval |
| Persistent 删除/写入 (API/命令) | pending_approval | allow + `approval.bypassed` 审计 | persistent workspace deletion/writing requires user approval |
| Profile 允许的工具 | allow | allow | tool allowed by capability profile |
| dangerous 白名单中的命令工具 (exec) | allow | allow | tool allowed by capability profile |

---

## 10. 测试覆盖

### 10.1 测试用例

| 测试名称 | 覆盖场景 |
|----------|----------|
| `test_pruned_tool_is_denied` | 永久裁剪工具 → Deny |
| `test_assistant_store_profile_overrides_request_profile` | Profile 优先级（助手 Profile 优于请求 Profile） |
| `test_sensitive_workspace_file_write_is_denied` | P0: 工作区敏感文件写入 → Deny |
| `test_container_sensitive_path_write_is_denied` | P0: 容器敏感路径写入 → Deny |
| `test_container_sensitive_path_write_is_denied_after_lexical_normalization` | P0: 路径词法规范化 |
| `test_persistent_delete_command_allows_without_approval` | dangerous: Persistent 删除（命令） → allow |
| `test_chained_persistent_delete_command_allows_without_approval` | dangerous: 命令链中的 Persistent 删除 → allow |
| `test_third_party_command_requires_admin_approval` | P1: 第三方命令 → admin 审批 |
| `test_chained_plugin_install_requires_admin_approval` | P1: 插件安装 → admin 审批 |
| `test_temp_workspace_command_is_allowed_by_dangerous_profile` | dangerous: 命令工具在 allowed_tools → allow |
| `test_write_auto_places_persistent_document_in_workspace` | P2: 自动放置 + Persistent 写入 → user 审批 |
| `test_write_auto_places_temp_smoke_output` | 自动放置 → temp 目录 → allow |
| `test_sensitive_workspace_file_is_not_rewritten_before_denial` | 敏感文件不被自动放置 → Deny |

### 10.2 运行测试

```bash
cd /path/to/ironagent
PYTHONPATH=. uv run pytest tests/security/test_capability_enforce.py -v
```

---

## 11. 配置参考

### 11.1 目录结构

```
config/
├── capabilities/
│   ├── chat.yaml
│   ├── onlyread.yaml
│   ├── onlywrite.yaml
│   └── dangerous.yaml
└── security/
    └── pruning.yaml
```

### 11.2 pruning.yaml 示例

```yaml
tools:
  - system_reset
  - unsafe_exec
  - factory_reset
```

---

## 12. 安全设计原则

1. **Fail-Closed（默认拒绝）**：未明确允许的操作一律拒绝
2. **优先级强制覆盖**：高优先级规则可一票否决低优先级规则
3. **上下文完整收集**：必须收集齐四维属性后才能决策
4. **路径规范化**：防止通过路径遍历绕过检查
5. **命令解析**：深度分析命令内容，不只是工具名

---

## 13. 扩展指南

### 13.1 添加新的高风险命令

在 `src/security/request_policy.py` 中修改：

```python
# 添加到 THIRD_PARTY_COMMANDS
THIRD_PARTY_COMMANDS = {
    ...
    "new_dangerous_command",
}

# 或添加新的命令分类
NEW_RISKY_COMMANDS = {"new_risky_tool"}
```

### 13.2 添加新的敏感路径

在 `src/security/request_policy.py` 中修改：

```python
# 添加到相应的前缀列表
CONTAINER_SENSITIVE_PREFIXES = ("/etc/", "/usr/local/bin/", "/app/", "/new/sensitive/")

# 或敏感文件名
SENSITIVE_WORKSPACE_BASENAMES = {".env", "USER.md", "AGENTS.md", "NEW_SENSITIVE"}
```

### 13.3 创建新的 Capability Profile

在 `config/capabilities/` 下创建新的 YAML 文件：

```yaml
name: myprofile
level: 2
allowed_tools:
  - read
  - write
approval_tools:
  - edit
description: My custom profile
```
