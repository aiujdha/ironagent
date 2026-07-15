# ADR 004: work_no_exec 能力档（可读写、禁 shell）

## Status

Accepted

## Context

文件总结等日常任务若使用 `dangerous` 能力档，助手读取大文件时可能在 `read` 返回被截断后，改用 `exec` 执行 `cat`、`sed`、`python` 等命令取回完整内容，绕开 `read` 的截断/分页边界。

这不是 `read` 工具本身绕过限制，而是能力档边界问题：安全服务的 profile 规则是工具命中 `allowed_tools` 就直接放行，命中 `approval_tools` 才进入审批。`dangerous` 档保留了 `exec`、`process`、`subagents`、`code_execution` 等高危工具（现行 `config/capabilities/dangerous.yaml` 将它们置于 `approval_tools`，需显式审批才可使用），日常读写任务不应依赖这条审批通道；而直接削弱 `dangerous`，会影响现有高危调试、进程诊断、脚本执行和演示场景。

## Decision

新增中间能力档 `work_no_exec`，用于“可读写但禁止 shell”的日常工作场景。能力边界：

- 允许：`read`、`write`、`edit`、`apply_patch`、web、memory、当前 session 状态/列表。
- 不含：`exec`、`process`、`cron`、`sessions_spawn`、`subagents`、`code_execution`。
- 不含 `sessions_history`，避免通过历史会话内容重新取回旧命令输出或大文件正文。
- `approval_tools` 为空，避免误以为 shell 可审批使用；这个 profile 下 shell 就是不允许。

使用划分：文件总结、资料整理、报告生成、轻量文件编辑使用 `work_no_exec`；纯只读问答继续使用 `onlyread`；确实需要 shell、进程、脚本、子代理的场景保留 `dangerous`（其下高危工具需审批）。

## Consequences

- 日常读写任务只能经受控的 `read` 分页读取文件，无法切换 shell 绕过截断。
- 使用 `work_no_exec` 的助手可读写文件，但 `exec` 等工具被 profile 直接拒绝，不进入审批。
- 使用 `dangerous` 的助手行为不变，高危场景不受影响。
- 内置 profile 顺序为 `chat -> onlyread -> onlywrite -> work_no_exec -> dangerous`，admin 侧提供 `work`、`no_exec`、`noexec`、`safe_write`、`safe-write` 别名归一化到 `work_no_exec`。
