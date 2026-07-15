# IronAgent 工作空间选择与文件落点设计

## 1. 文档目标

本文档定义两项新增能力的可实施方案：

1. 用户可以为每个助手选择公共工作空间。
2. 系统能够在运行时判断文件应放入 `temp/` 还是 `persistent/`。

本文档不是概念讨论，而是面向实现的设计稿。重点说明：

- 需要新增哪些数据结构
- 需要修改哪些模块
- 需要如何兼容现有助手
- 需要怎样分阶段上线

## 2. 当前现状

当前实现已经具备以下基础：

- 助手运行时会自动创建工作空间：
  - `USER.md`
  - `AGENTS.md`
  - `temp/`
  - `persistent/`
  - `skills/`
- 当前工作空间根目录是全局配置：
  - `src/shared/config.py`
  - `assistants_workspace_root`
- 当前路径策略能识别：
  - `workspace_root`
  - `workspace_temp`
  - `workspace_persistent`
  - `workspace_sensitive`
  - `container_sensitive`
  - `container_secret`
- 当前权限规则已经支持：
  - `temp/` 默认读写删放行
  - `persistent/` 删除走用户审批
  - `AGENTS.md` / `USER.md` / `.env` 写删拒绝

本文档立项时缺失的两件关键能力，现已实现：

1. 按助手选择宿主机工作空间根目录：`Assistants` 页面可从允许列表（`IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON`，见 `src/shared/workspace_roots.py`）中选择根目录，`provisioner` 按助手的 `workspace_config` 解析实际路径。
2. 写文件时自动决定进入 `temp/` 还是 `persistent/`：`write`、`edit`、`apply_patch` 已接入自动落点（`src/workspace/placement.py`），实现状态详见 §11.3。

以下章节保留为落点判定与工作空间选择的规则规格。

## 3. 设计原则

### 3.1 工作空间选择要“可控自由”

不建议让用户直接输入宿主机任意绝对路径。

在当前“控制面也运行在 Docker 容器中”的模式下，任意路径会引入两个问题：

- `security-service` 容器未必能访问该路径
- 动态编排 IronAgent 助手容器时，路径挂载关系会变得不可控

因此推荐方案是：

- 系统维护一组“允许的工作空间根目录”
- 用户为每个助手选择其中一个根目录
- 用户再填写或确认助手目录名

最终真实路径为：

```text
<selected_root>/<assistant_workspace_slug>
```

### 3.2 文件落点不能完全依赖模型猜测

不能把“该放 `temp/` 还是 `persistent/`”完全交给模型自行理解。

更稳的方式是增加一层显式策略：

1. 用户显式指定路径时，优先尊重用户。
2. skill/plugin 可以给出保留策略提示。
3. 系统用可解释规则自动判断。
4. 判断不出来时，默认放入 `temp/`。

### 3.3 统一目录结构

无论用户选择哪个工作空间根目录，每个助手内部都统一生成：

```text
<workspace_root>/
  USER.md
  AGENTS.md
  temp/
  persistent/
  skills/
```

这样可以保证：

- Hook 判定稳定
- 技能发现稳定
- 审批规则稳定
- 文档与验收流程稳定

## 4. 目标行为

### 4.1 用户选择工作空间

在 `Assistants` 页面创建或编辑助手时，用户应能配置：

- 工作空间来源：
  - 系统默认根目录
  - 允许列表中的某个自定义根目录
- 助手工作空间目录名
- `temp` 目录名
- `persistent` 目录名

第一阶段建议固定目录名为：

- `temp`
- `persistent`

先不要开放自定义目录名，以降低复杂度。

### 4.2 自动判断文件落点

系统需要对“写入类动作”增加落点判断。

典型场景：

- 用户让助手生成正式报告
  - 输出进入 `persistent/`
- 助手执行中间转换、临时草稿、测试文件
  - 输出进入 `temp/`
- 用户没有写明目录，系统自动补齐前缀

例如：

- 输入：`report.docx`
- 输出：`persistent/report.docx`

例如：

- 输入：`smoke-result.json`
- 输出：`temp/smoke-result.json`

## 5. 数据结构改造

### 5.1 新增助手工作空间配置

建议在 `src/runtime/models.py` 新增：

```python
WorkspaceSelectionMode = Literal["default_root", "managed_root"]


class AssistantWorkspaceConfig(BaseModel):
    selection_mode: WorkspaceSelectionMode = "default_root"
    root_id: str = "default"
    workspace_slug: str = ""
    auto_place_outputs: bool = True
```

说明：

- `selection_mode`
  - 当前阶段保留两个值即可，后面可扩展
- `root_id`
  - 对应允许列表中的某个工作空间根
- `workspace_slug`
  - 工作空间目录名
  - 默认使用 `assistant_id`
- `auto_place_outputs`
  - 是否启用自动落点
  - 默认开启

### 5.2 扩展助手记录

在 `src/assistants/models.py` 中给 `AssistantRecord` 增加：

```python
workspace_config: AssistantWorkspaceConfig = Field(
    default_factory=AssistantWorkspaceConfig
)
```

### 5.3 扩展运行时记录

在 `src/runtime/models.py` 的 `AssistantRuntimeRecord` 中增加：

```python
workspace_root_id: str | None = None
workspace_slug: str | None = None
temp_dir: str = "temp"
persistent_dir: str = "persistent"
```

这样可以在后台和审计里明确看到当前助手使用的是哪一个工作空间根，以及实际目录名。

## 6. 配置层改造

### 6.1 新增允许的工作空间根目录

在 `src/shared/config.py` 增加：

```python
allowed_workspace_roots_json: str = ""
default_workspace_root_id: str = "default"
```

推荐再增加一个解析后的结构：

```python
class WorkspaceRootEntry(BaseModel):
    id: str
    label: str
    host_path: Path
```

建议把 JSON 解析逻辑放到单独 helper 中，例如：

- `src/shared/workspace_roots.py`

### 6.2 推荐配置格式

推荐在 `.env` 中维护：

```text
IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON=[
  {"id":"default","label":"默认工作空间","host_path":"/srv/ironagent/workspaces"},
  {"id":"finance","label":"财务工作空间","host_path":"/srv/finance-assistants"}
]
IRON_AGENT_DEFAULT_WORKSPACE_ROOT_ID=default
```

### 6.3 Docker 挂载约束

若控制面运行在 Docker 中，则这些工作空间根目录必须被同路径挂载进入：

- `security-service`
- `admin`

否则即使配置存在，也无法真正创建或管理工作空间。

## 7. 运行时编排改造

### 7.1 `src/runtime/provisioner.py`

当前 `_ensure_workspace()` 直接写死：

```python
workspace_root = assistants_workspace_root / assistant_id
```

需要改为：

1. 根据 `assistant.workspace_config.root_id` 找到实际宿主机根目录
2. 根据 `workspace_slug` 计算实际工作空间目录
3. 创建标准目录结构
4. 把结果写回运行时记录

建议新增方法：

- `_resolve_workspace_root(...)`
- `_sanitize_workspace_slug(...)`
- `_ensure_workspace_layout(...)`

### 7.2 兼容旧助手

旧助手没有 `workspace_config` 时：

- 默认使用 `default_root`
- 默认 `workspace_slug = assistant_id`

这样不用做数据迁移脚本，也能平滑兼容。

## 8. 自动落点策略设计

### 8.1 新增模块

建议新增：

- `src/workspace/placement.py`
- `src/workspace/models.py`

核心结构建议如下：

```python
PlacementTarget = Literal["temp", "persistent", "unchanged"]


class PlacementHint(BaseModel):
    retention: Literal["auto", "temp", "persistent"] = "auto"
    explicit_path: bool = False


class PlacementDecision(BaseModel):
    target: PlacementTarget
    original_path: str
    rewritten_path: str
    reason: str
```

### 8.2 输入信息

落点策略至少要拿到：

- `assistant`
- `tool_name`
- `path`
- `filename`
- `args`
- `skill_name`
- `retention_hint`

### 8.3 判定优先级

建议顺序：

1. 用户路径已明确包含 `temp/` 或 `persistent/`
   - 直接保留
2. skill/plugin 提供 `retention_hint`
   - 按 hint 走
3. 系统根据规则自动判断
4. 无法判断时默认 `temp`

### 8.4 第一版自动判断规则

默认进入 `temp/`：

- 文件名包含：
  - `tmp`
  - `temp`
  - `draft`
  - `smoke`
  - `cache`
- 扩展名包含：
  - `.tmp`
  - `.log`
  - `.cache`
  - `.part`
- 中间转换产物
- 校验副本

默认进入 `persistent/`：

- 用户明确说“保存”“保留”“导出”
- 文件名明显是最终交付物：
  - `report.docx`
  - `summary.xlsx`
  - `invoice.pdf`
- skill 明确声明这是“final output”

## 9. Hook 与权限策略改造

### 9.1 写入类工具增加路径重写

对这些工具，建议在进入实际执行前先走自动落点：

- `write`
- `edit`
- `apply_patch`

执行顺序建议为：

1. 路径归一化
2. 落点策略判断
3. 若需重写，改成 `temp/...` 或 `persistent/...`
4. 再走现有权限判定

### 9.2 对 `exec` 保守处理

不要对任意 shell 命令做激进重写。

对 `exec` 更稳的策略是：

- 分析命令中涉及的目标路径
- 识别路径类别
- 走审批或拒绝
- 对已知 skill 命令，再做参数级注入

### 9.3 `src/security/request_policy.py` 需要调整的点

当前 `_classify_path()` 已经能区分：

- `temp`
- `persistent`
- `workspace_sensitive`

后续需要配合：

1. 使用助手自己的 `runtime.workspace_root`
2. 识别重写后的目标路径
3. 对“自动落点前路径”和“重写后路径”都保留审计信息

建议在 `metadata` 中增加：

- `original_path`
- `effective_path`
- `placement_target`
- `placement_reason`

## 10. Skill / Plugin 侧协作方式

### 10.1 为 skill 增加保留提示

建议后续给 `office-docs`、`local-pdf` 等 skill 增加显式参数：

- `retention=auto`
- `retention=temp`
- `retention=persistent`

这样生成正式文档时可以稳定进入 `persistent/`，烟测产物则可以继续进入 `temp/`。

### 10.2 不依赖模型自行记忆规则

不要只靠 `USER.md` 或 prompt 提示模型“你应该把什么放在 temp”。

模型提示可以保留，但真正的落点决策应以 IronAgent 的策略层为准。

## 11. Admin 页面改造

### 11.1 `src/admin/pages/assistants.py`

建议新增这些表单项：

- `Workspace root`
  - 下拉选择允许列表中的根目录
- `Workspace directory name`
  - 文本输入
- `Auto place outputs`
  - 开关

列表页建议新增展示列：

- `workspace_root_id`
- `workspace_slug`
- `workspace_root`

### 11.2 页面行为

保存助手时需要：

1. 校验 `workspace_slug`
2. 校验所选根目录存在
3. 校验根目录位于允许列表
4. 重新 provision 助手容器

### 11.3 当前实现状态

当前已经完成：

- 每个助手单独选择 `Workspace root`
- 每个助手单独保存 `Workspace directory name`
- `Auto place outputs` 配置持久化
- `write`、`edit`、`apply_patch` 的自动落点

当前自动落点规则：

- 如果模型已经显式写到 `temp/` 或 `persistent/`，保持原样
- 如果目标文件已存在，保持原样
- 如果命中敏感文件名，例如 `USER.md`、`AGENTS.md`、`.env`，不做改写，继续走安全拦截
- 新生成的正式文档类输出优先落到 `persistent/`
- `smoke`、`tmp`、`draft`、`log` 等临时产物优先落到 `temp/`
- 无法明确判断时，默认落到 `temp/`

## 12. 推荐实施顺序

### P0

- 为助手增加 `workspace_config`
- 增加允许工作空间根目录配置
- 让每个助手可以选择自己的工作空间根目录
- 让 `provisioner` 按新配置创建工作空间

### P1

- 增加 `WorkspacePlacementPolicy`
- 先接入 `write/edit/apply_patch`
- 增加路径重写审计字段

### P2

- 给 `office-docs`、`local-pdf` 增加 `retention` 支持
- 在 skill 调用链中传递 placement hint

### P3

- 再评估是否要对 `exec` 做更深的自动落点支持
- 补充 `temp` 清理任务与更细的工作区生命周期管理

## 13. 验收标准

完成后至少应满足：

1. 不同助手可以选择不同的工作空间根目录。
2. 每个工作空间都会生成标准结构：
   - `USER.md`
   - `AGENTS.md`
   - `temp/`
   - `persistent/`
   - `skills/`
3. 用户未显式指定目录时，`write/edit/apply_patch` 能自动进入正确目录。
4. 中间产物默认进入 `temp/`。
5. 正式交付物默认进入 `persistent/`。
6. 删除 `persistent/` 文件仍走用户审批。
7. 修改 `USER.md`、`AGENTS.md`、`.env` 仍被拒绝。
8. 审计日志能同时看到原始路径和最终生效路径。

## 14. 一句话结论

这项能力的正确实现方式不是“让模型自己猜文件该放哪里”，而是：

- 用户选择受控的工作空间根目录
- 系统生成统一的工作空间结构
- IronAgent 在写入前根据策略自动决定 `temp/` 或 `persistent/`
- 权限、审批、审计继续围绕重写后的实际路径生效
