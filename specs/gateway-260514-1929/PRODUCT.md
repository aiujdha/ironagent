# gateway-260514-1929: Control UI session minimum loop

- 模块：gateway
- 创建日期：2026-05-14
- 编写人：kolbaaa14-gif
- 最近修改人：kolbaaa14-gif
- 最近修改日期：2026-05-18
- 等级：S2
- 状态：ready

## 背景

当前 WebUI 需要从管理后台打开助手控制页，并在控制页中完成 workspace 文件访问和用户审批处理。内网浏览器安全策略更严格，key、token 不能可靠带到新页面，页面内 token 也可能在十几分钟内失效。继续依赖 URL、hash、`sessionStorage` 或前端 header 携带管理凭证，会导致新窗口丢凭证、凭证过期、跨页面泄漏和审批/workspace 状态不一致。

本任务先做最小闭环：`launch_ticket -> HttpOnly Cookie -> control/api`。后续再把 workspace-files 和审批 resolve 接入同一个 control session。

Go Gateway 多实例 runtime registry 的 shared MySQL、heartbeat、TTL、shared secrets 和 unsupported no-DB multi-node 边界由 `specs/runtime-260518-1025/` 管理。本 spec 仍只覆盖控制页会话最小闭环。

## 摘要

问题：控制页仍存在前端携带或缓存 token 的路径，workspace-files 和审批 resolve 也没有统一收口到同一条控制面会话。期望结果：用户从管理后台打开控制页后，只通过同源 `control/api` 和 HttpOnly 会话完成受控操作，浏览器页面不再直接持有 admin/runtime key 或 token。

## 问题

- 新页面无法稳定继承 key、token，导致 workspace 文件和审批接口在内网环境中失败。
- 页面 token 有效期短，用户停留一段时间后可能出现上传、下载、删除、审批 resolve 失败。
- URL/hash/前端 storage/header 传递管理凭证会扩大泄漏面，不适合作为控制面长期方案。
- workspace-files、审批 resolve 与控制页连接信息不是同一个会话来源，可能出现一个操作已成功、另一个操作仍按旧凭证失败的状态。
- 当前用户审批弹窗只改变了交互形态，审批 resolve 的服务端会话边界仍需要单独收口。

## 参与方

- 管理员：从管理后台打开指定助手的控制页，并拥有发起控制页会话的权限。
- 终端用户：在控制页中查看 workspace 文件、上传/删除文件、处理用户审批弹窗。
- API 调用方：管理后台或调用方只获取一次性 launch ticket，不直接把内部服务凭证或 runtime token 交给浏览器页面。
- 助手 runtime：继续通过既有运行时协议处理任务，不接收浏览器直接携带的管理凭证。
- Workspace：文件列表、上传、下载、删除必须绑定到 control session 内的 assistant scope。
- Skill/plugin：用户审批来源仍可来自插件或运行时，但 resolve 必须经过 control session 授权。
- 内部服务：安全服务、管理后台、Gateway 按内部调用边界交换真实凭证，浏览器不可见。

## 生效时机

- 发布后生效：新打开的控制页使用 `launch_ticket` 换取 HttpOnly Cookie，并清理 URL 中的 ticket。
- 立即生效：同一浏览器会话内后续 `control/api` 请求自动携带 HttpOnly Cookie。
- 重启后生效：如果实现引入 Gateway 内存态 session 或配置项，Gateway 重启会清空未持久化会话和未使用 ticket。

## 权限与安全边界

- 谁可以操作：具备管理后台授权的管理员可以创建 launch ticket；持有有效 control session 的控制页可以访问自己绑定的助手控制接口。
- 谁可以查看：控制页只能查看 session 绑定助手的 workspace 和用户审批状态；后台管理员仍通过管理后台查看系统/管理员审批。
- 谁不能触发：未登录管理后台、无效 ticket、过期 ticket、重复使用 ticket、assistant 不匹配、跨站来源请求不能触发控制面写操作。
- 是否需要审计：需要。ticket 创建、ticket resolve、workspace 写操作、审批 resolve、失败的权限校验都要具备可追踪日志或审计记录。

## 非目标

- 本 spec 不做系统/管理员审批弹窗迁移；系统/管理员审批仍走管理后台审批。
- 本 spec 不扩大外网暴露面，不新增浏览器可见的内部服务凭证、gateway token 或 runtime token。
- 本 spec 不重做 workspace 存储模型、文件落点或对象存储同步策略。
- 本 spec 不改变助手 runtime 的任务执行协议。
- 本 spec 不引入长期可复用的页面 token 作为替代方案。

## 行为

B1. 当管理员从管理后台打开助手控制页时，后台只生成一次性 `launch_ticket`，控制页 URL 不携带内部服务凭证、gateway token、runtime token 或 workspace token。

B2. 当控制页加载并携带有效 `launch_ticket` 时，Gateway 校验 ticket 的有效期、来源、assistant scope 和使用状态，创建 control session，设置 HttpOnly Cookie，并把页面重定向到不含 ticket 的干净 URL。

B3. 当 `launch_ticket` 过期、已使用、assistant 不匹配或来源校验失败时，Gateway 必须失败关闭，不创建 control session，控制页提示用户从管理后台重新打开。

B4. 当控制页调用 `/assistants/{assistant_id}/control/api/v1/**` 时，浏览器只依赖同源 HttpOnly Cookie；前端代码不得读取、保存或发送内部服务凭证、gateway token、runtime token。

B5. 当 control session 过期、丢失或被 Gateway 重启清空时，`control/api` 返回明确的 401/403，控制页不得静默降级到 URL/hash/storage token，应提示从管理后台重新打开。

B6. 当用户查看、上传、下载或删除 workspace 文件时，所有 workspace-files 请求必须经过同一个 control session，并校验 session 绑定的 assistant scope。

B7. 当用户在审批弹窗点击“允许 / 拒绝”时，审批 resolve 必须经过同一个 control session，并校验审批归属、assistant scope、审批状态和决策权限。

B8. 当存在多条用户审批时，前端可以继续按当前交互逐条弹出；每条审批 resolve 的服务端请求都必须使用 control session，处理完当前条后再进入下一条。

B9. 当系统/管理员审批出现时，控制页不得在用户弹窗中处理，仍提示或引导到管理后台审批流程。

B10. 当 Gateway 需要调用安全服务、管理后台、runtime 或 workspace 内部接口时，真实 key/token 只能留在服务端内部交换，不返回给浏览器 JSON、HTML、日志可见字段或前端 storage。

B11. 当部署在内网 HTTP、HTTPS 反代或 NodePort 形态下时，Cookie 的 `Secure`、`SameSite`、domain/path 策略必须与实际同源入口匹配，不能因为错误判断协议导致控制页无法建立 session。

B12. 当回滚本变更时，应能关闭 control session 路径并恢复到旧入口，但不得把真实 token 泄漏到历史记录、日志或新页面；回滚方案必须说明旧路径只用于应急和受控环境。

## 已确认决策

- 当前项目 `launch_ticket` 默认有效期为 600 秒，可通过 `IRON_AGENT_CONTROL_UI_LAUNCH_TICKET_TTL_SECONDS` 覆盖；测试或部署可显式覆盖为更短 TTL。
- control session 先用 Gateway 内存态实现；Gateway 重启后会话失效，控制页需要从管理后台重新打开。
- Cookie 写操作先采用同源防护：当请求带 `Origin` 或 `Referer` 时必须与 Gateway 入口同源。
- 旧 `/assistants/{id}/api/v1/**` 兼容路径暂不删除；新控制页入口统一使用 `/assistants/{id}/control/api/v1/**`。
