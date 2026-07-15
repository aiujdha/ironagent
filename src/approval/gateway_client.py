"""
Gateway WebSocket 客户端

用于在审批解决后通知 IronAgent Gateway。
Gateway 通过 WebSocket 接收事件并转发给相应的客户端。

重要：IronAgent Gateway 的 WebSocket 协议只接受两种帧格式：
- type: "req" — 请求帧，用于调用 Gateway 方法（如 exec.approval.resolve）
- type: "event" — 事件帧，仅由 Gateway 服务端广播给客户端，外部客户端不能发送

因此，通知审批解决必须使用 "req" 帧调用 exec.approval.resolve 方法，
而不是发送 "event" 帧。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import websockets
from websockets.asyncio.client import connect

from src.shared.config import get_settings
from src.shared.logging import get_logger

log = get_logger(__name__)

# IronAgent Gateway 协议版本（必须与 Gateway 期望的版本匹配）
GATEWAY_PROTOCOL_VERSION = 3
GATEWAY_CLIENT_ID = "ironagent-tui"
GATEWAY_CLIENT_MODE = "ui"
GATEWAY_APPROVAL_SCOPES = ["operator.approvals"]


def _build_connect_params(auth_token: str) -> dict[str, Any]:
    return {
        "minProtocol": GATEWAY_PROTOCOL_VERSION,
        "maxProtocol": GATEWAY_PROTOCOL_VERSION,
        "client": {
            "id": GATEWAY_CLIENT_ID,
            "displayName": "IronAgent Security Service",
            "mode": GATEWAY_CLIENT_MODE,
            "version": "1.0.0",
            "platform": "server",
        },
        "role": "operator",
        "scopes": list(GATEWAY_APPROVAL_SCOPES),
        "caps": ["tool-events"],
        "auth": {
            "token": auth_token,
        },
        "userAgent": "IronAgent Security Service",
        "locale": "zh-CN",
    }


def _normalize_ws_url(url: str | None) -> str | None:
    """将 http:// URL 转换为 ws:// URL，并处理容器内访问 host 的问题"""
    if not url:
        return None
    url = url.strip()
    # 将 http:// 转换为 ws://
    if url.startswith("http://"):
        url = "ws://" + url[7:]
    elif url.startswith("https://"):
        url = "wss://" + url[8:]

    # 如果是 127.0.0.1 或 localhost，改为 host.docker.internal（容器内访问 host）
    if "127.0.0.1" in url or "localhost" in url:
        import re
        # 替换 127.0.0.1 或 localhost 为 host.docker.internal
        url = re.sub(r"://(127\.0\.0\.1|localhost):", "://host.docker.internal:", url)

    return url


async def _get_gateway_url_for_assistant(assistant_id: str | None) -> tuple[str | None, str | None]:
    """
    根据 assistant_id 获取对应的 Gateway URL 和 auth token。

    从 AssistantService 获取助手的 runtime 配置，提取 gateway_url 和 gateway_token。
    返回 (gateway_url, gateway_token) 元组。
    """
    if not assistant_id:
        return None, None

    try:
        from src.assistants.service import AssistantService
        service = AssistantService()
        # 优先使用异步方法
        async_loader = getattr(service, "aget_assistant", None)
        if callable(async_loader):
            assistant = await async_loader(assistant_id)
        else:
            assistant = service.get_assistant(assistant_id)
        if assistant and hasattr(assistant, 'runtime') and assistant.runtime:
            url = _normalize_ws_url(assistant.runtime.gateway_url)
            token = getattr(assistant.runtime, 'gateway_token', None)
            return url, token
    except Exception as exc:
        log.warning("failed_to_get_gateway_url", assistant_id=assistant_id, error=str(exc))

    return None, None


class GatewayClient:
    """Gateway WebSocket 客户端（持久连接）"""

    def __init__(self, gateway_url: str | None = None, gateway_token: str | None = None) -> None:
        if gateway_url:
            self.url = _normalize_ws_url(gateway_url) or gateway_url
        else:
            settings = get_settings()
            host = settings.iron_agent_gateway_host
            port = settings.iron_agent_gateway_port
            self.url = f"ws://{host}:{port}"
        self._auth_token = gateway_token
        self._connected = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._handshaked = False
        self._event_reader_task: asyncio.Task | None = None
        # 消息队列：后台读取器放入，send_request 取出
        self._message_queue: asyncio.Queue[str] = asyncio.Queue()
        self._reader_lock = asyncio.Lock()

    async def connect(self) -> bool:
        """建立 WebSocket 连接"""
        if self._connected and self._ws is not None and self._handshaked:
            try:
                if self._ws.state == websockets.State.OPEN:
                    return True
            except Exception:
                pass
            self._connected = False
            self._handshaked = False
            self._ws = None

        try:
            self._ws = await connect(self.url, open_timeout=5.0)
            self._connected = True
            self._handshaked = False
            # 清空旧消息
            while not self._message_queue.empty():
                self._message_queue.get_nowait()
            log.info("gateway_client_connected", url=self.url)
            return True
        except Exception as exc:
            log.warning("gateway_client_connect_failed", url=self.url, error=str(exc))
            self._connected = False
            self._handshaked = False
            self._ws = None
            # 连接失败时，从全局缓存中移除自身，以便下次使用新 URL 重试
            _remove_client_from_cache(self.url)
            return False

    async def ensure_connected(self) -> bool:
        """确保连接和握手完成，如果断开则重连"""
        if await self.connect() and await self._perform_handshake():
            # 启动后台事件读取器
            if self._event_reader_task is None or self._event_reader_task.done():
                self._event_reader_task = asyncio.create_task(self._background_event_reader())
            return True
        return False

    async def _background_event_reader(self) -> None:
        """后台读取所有 WebSocket 消息，放入队列，并处理审批解决事件"""
        try:
            while self._ws and self._ws.state == websockets.State.OPEN:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=60.0)
                await self._message_queue.put(raw)
                # 处理 exec.approval.resolved 事件：同步 IronAgent 数据库
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "event" and msg.get("event") == "exec.approval.resolved":
                        await _handle_approval_resolved_event(msg.get("payload", {}))
                except Exception:
                    pass  # 解析失败不影响主流程
        except TimeoutError:
            # recv 超时，连接可能还活着，重新启动读取
            if self._ws and self._ws.state == websockets.State.OPEN:
                self._event_reader_task = asyncio.create_task(self._background_event_reader())
        except Exception as exc:
            log.debug("gateway_event_reader_stopped", error=str(exc))
            self._connected = False
            self._handshaked = False

    async def _read_message(self, timeout: float = 10.0) -> dict[str, Any] | None:
        """从消息队列中读取下一条消息，带超时"""
        try:
            raw = await asyncio.wait_for(self._message_queue.get(), timeout=timeout)
            return json.loads(raw)
        except TimeoutError:
            return None

    async def disconnect(self) -> None:
        """关闭 WebSocket 连接"""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._connected = False
            self._handshaked = False

    async def _perform_handshake(self) -> bool:
        """
        执行 Gateway WebSocket 握手。

        IronAgent Gateway 的握手流程是两步：
        1. 连接后 Gateway 自动发送 connect.challenge 事件（含 nonce）
        2. 客户端收到 nonce 后，发送 connect 请求帧
        3. Gateway 回复 { type: "res", ok: true, payload: { type: "hello-ok", ... } }
        """
        if self._handshaked:
            return True

        if self._ws is None:
            return False

        try:
            # Step 1: 读取 connect.challenge 事件（握手阶段直接读取）
            raw_challenge = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            challenge = json.loads(raw_challenge)

            if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
                log.warning(
                    "gateway_handshake_unexpected_message",
                    expected="connect.challenge",
                    got_type=challenge.get("type"),
                    got_event=challenge.get("event"),
                )
                return False

            nonce = (challenge.get("payload") or {}).get("nonce", "")
            if not nonce:
                log.warning("gateway_handshake_missing_nonce")
                return False

            # Step 2: 发送 connect 请求帧
            connect_frame = {
                "type": "req",
                "id": str(uuid4()),
                "method": "connect",
                "params": _build_connect_params(self._auth_token or ""),
            }
            await self._ws.send(json.dumps(connect_frame))

            # Step 3: 等待握手响应（握手阶段直接读取）
            raw_response = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            response = json.loads(raw_response)

            if response.get("ok"):
                self._handshaked = True
                log.info("gateway_handshake_success")
                return True
            else:
                error = response.get("error", {})
                log.warning(
                    "gateway_handshake_failed",
                    error_code=error.get("code"),
                    error_message=error.get("message"),
                )
                return False
        except TimeoutError:
            log.warning("gateway_handshake_timeout")
            return False
        except Exception as exc:
            log.warning("gateway_handshake_error", error=str(exc))
            self._connected = False
            self._handshaked = False
            self._ws = None
            return False

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """
        发送请求帧到 Gateway 并等待响应。

        使用 IronAgent Gateway 的 req/res 协议：
        - 发送 { type: "req", id: "...", method: "...", params: {...} }
        - 接收 { type: "res", id: "...", ok: true/false, payload: {...} }

        Args:
            method: Gateway 方法名，如 "exec.approval.resolve"
            params: 方法参数
            timeout: 等待响应的超时时间（秒）

        Returns:
            响应 payload（成功时）或 None（失败时）
        """
        if not await self.ensure_connected():
            log.warning("gateway_request_send_failed", method=method, reason="not_connected")
            return None

        if self._ws is None:
            return None

        request_id = str(uuid4())
        frame = {
            "type": "req",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        try:
            await self._ws.send(json.dumps(frame))
            log.info("gateway_request_sent", method=method, request_id=request_id)

            # 等待响应 — 从消息队列中读取，跳过事件帧，匹配对当前请求的响应
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError()
                message = await self._read_message(timeout=min(remaining, 30.0))
                if message is None:
                    continue  # 超时但未到 deadline，继续

                # 跳过事件帧
                if message.get("type") == "event":
                    evt_name = message.get("event", "")
                    if "approval" in evt_name:
                        log.info("gateway_event_received", event_name=evt_name)
                    continue

                # 检查是否是对我们请求的响应
                if message.get("type") == "res" and message.get("id") == request_id:
                    if message.get("ok"):
                        log.info(
                            "gateway_request_success",
                            method=method,
                            request_id=request_id,
                        )
                        return message.get("payload")
                    else:
                        error = message.get("error", {})
                        log.warning(
                            "gateway_request_error",
                            method=method,
                            request_id=request_id,
                            error_code=error.get("code"),
                            error_message=error.get("message"),
                            raw_error=error if error else message,
                        )
                        return None

                # 其他消息（不匹配的 res 等），继续等待
                log.debug(
                    "gateway_unexpected_message",
                    msg_type=message.get("type"),
                    msg_id=message.get("id"),
                    expected_id=request_id,
                )
        except TimeoutError:
            log.warning("gateway_request_timeout", method=method, request_id=request_id)
            return None
        except ConnectionError as exc:
            log.warning("gateway_request_connection_error", method=method, error=str(exc))
            self._connected = False
            self._handshaked = False
            return None
        except Exception as exc:
            log.warning("gateway_request_error", method=method, error=str(exc))
            return None

    async def send_event(self, event: str, payload: dict[str, Any] | None = None) -> bool:
        """
        发送事件到 Gateway（用于广播通知，如 exec.approval.requested）。

        注意：Gateway 只接受 type: "req" 帧，event 帧会被丢弃。
        此方法保留用于向后兼容，但内部已改为使用 req 帧调用
        对应的 Gateway 方法来实现事件广播效果。
        对于 exec.approval.resolved，应使用 resolve_approval() 方法。

        Args:
            event: 事件名称，如 "exec.approval.requested"
            payload: 事件负载数据

        Returns:
            是否发送成功
        """
        # exec.approval.requested 使用 event 帧广播 — 但 Gateway 不接受外部 event 帧
        # 所以这里也改用 req 帧的方式：调用 exec.approval.request 方法
        if event == "exec.approval.requested":
            return await self._request_approval_via_gateway(payload or {})

        # 其他事件使用旧方式（但很可能被 Gateway 丢弃）
        if not await self.connect():
            log.warning("gateway_event_send_failed", event=event, reason="not_connected")
            return False

        if self._ws is None:
            return False

        try:
            frame: dict[str, Any] = {"type": "event", "event": event}
            if payload is not None:
                frame["payload"] = payload
            await self._ws.send(json.dumps(frame))
            log.info("gateway_event_sent", event=event, payload=payload)
            return True
        except Exception as exc:
            log.warning("gateway_event_send_error", event=event, error=str(exc))
            self._connected = False
            self._handshaked = False
            self._ws = None
            return False

    async def _request_approval_via_gateway(self, payload: dict[str, Any]) -> bool:
        """
        通过 Gateway 的 exec.approval.request 方法注册审批请求。

        这会在 Gateway 的 ExecApprovalManager 中创建一个 pending 条目，
        使得后续的 exec.approval.resolve 调用能够正确解除等待。
        """
        result = await self.send_request("exec.approval.request", payload)
        return result is not None

    async def resolve_approval(
        self,
        approval_id: str,
        decision: str,
    ) -> bool:
        """
        通过 Gateway 的 exec.approval.resolve 方法解决审批。

        这是正确的方式：使用 req 帧调用 Gateway 方法，
        Gateway 会：
        1. 在 ExecApprovalManager 中 resolve 对应的 pending 条目
        2. 广播 exec.approval.resolved 事件给所有客户端
        3. 解除等待该审批的 Promise，使命令继续执行

        Args:
            approval_id: 审批 ID（必须与 exec.approval.request 注册时一致）
            decision: 审批决定，"allow-once"、"allow-always" 或 "deny"

        Returns:
            是否解决成功
        """
        params = {
            "id": approval_id,
            "decision": decision,
        }
        result = await self.send_request("exec.approval.resolve", params)
        return result is not None


async def _handle_approval_resolved_event(payload: dict[str, Any]) -> None:
    """
    处理 Gateway 广播的 exec.approval.resolved 事件。

    当用户在 IronAgent UI 中点击 Allow/Deny 时，Gateway 会广播此事件。
    IronAgent 需要同步更新数据库，以便 bridge 轮询时能看到状态变更。
    """
    approval_id = payload.get("id")
    decision = payload.get("decision")
    if not approval_id or not decision:
        return

    # 将 IronAgent 的 decision 映射为 IronAgent 的 resolution
    # IronAgent: "allow-once" / "allow-always" / "deny"
    # IronAgent: "allow" / "deny"
    if decision in ("allow-once", "allow-always"):
        resolution = "allow"
    elif decision == "deny":
        resolution = "deny"
    else:
        log.warning("gateway_unknown_decision", approval_id=approval_id, decision=decision)
        return

    # 检查 IronAgent 数据库中该审批是否仍然是 pending 状态
    # 如果已经是 approved/denied（比如管理员已在后台审批），则跳过
    try:
        from src.approval.service import ApprovalService
        service = ApprovalService()
        existing = await service.aget_request(approval_id)
        if existing is None:
            log.debug("gateway_approval_not_found", approval_id=approval_id)
            return
        if existing.status != "pending":
            log.debug(
                "gateway_approval_already_resolved",
                approval_id=approval_id,
                status=existing.status,
            )
            return

        is_allow_always = decision == "allow-always"

        # 如果是 allow-always，标记该会话为自动放行
        # 这与 routes.py 中 resolve_approval() 的逻辑保持一致
        if is_allow_always:
            from src.security_service.session_auto_allow import handle_allow_always
            handle_allow_always(approval_id)

        resolved = await service.aresolve_request(
            approval_id, resolution, is_allow_always=is_allow_always, skip_gateway_notify=True
        )
        if resolved:
            log.info(
                "gateway_approval_synced",
                approval_id=approval_id,
                resolution=resolution,
                source="exec.approval.resolved",
            )
    except Exception as exc:
        log.warning("gateway_approval_sync_failed", approval_id=approval_id, error=str(exc))


# 全局缓存：以 URL 为 key 的持久客户端
_gateway_clients: dict[str, GatewayClient] = {}


def _remove_client_from_cache(url: str) -> None:
    """从全局缓存中移除指定 URL 的客户端，以便下次使用新 URL 重试"""
    global _gateway_clients
    if url in _gateway_clients:
        del _gateway_clients[url]
        log.info("gateway_client_removed_from_cache", url=url)


def get_gateway_client(
    gateway_url: str | None = None,
    gateway_token: str | None = None,
) -> GatewayClient:
    """
    获取 Gateway 客户端（持久连接，按 URL 缓存）

    同一个 URL 复用同一个客户端实例，保持长连接。
    这样 Gateway 会在 hasExecApprovalClients 中识别到 IronAgent 的连接。

    Args:
        gateway_url: 可选的 Gateway URL。
        gateway_token: 可选的 Gateway 认证 token。
    """
    global _gateway_clients
    settings = get_settings()
    default_url = f"ws://{settings.iron_agent_gateway_host}:{settings.iron_agent_gateway_port}"
    key = gateway_url or default_url

    if key not in _gateway_clients:
        _gateway_clients[key] = GatewayClient(gateway_url, gateway_token=gateway_token)
    return _gateway_clients[key]


async def notify_approval_resolved(
    approval_id: str,
    tool_name: str,
    assistant_id: str | None,
    resolution: str,  # "allow" or "deny"
    gateway_url: str | None = None,
    gateway_token: str | None = None,
    *,
    is_allow_always: bool = False,
) -> bool:
    """
    通知 Gateway 审批已解决

    通过 Gateway 的 exec.approval.resolve 方法解决审批，
    这样 Gateway 会：
    1. 解除等待该审批的 Promise（使命令继续执行或被拒绝）
    2. 广播 exec.approval.resolved 事件给所有客户端

    注意：需要审批 ID 与之前通过 exec.approval.request 注册的 ID 一致。

    Args:
        approval_id: 审批 ID（必须与 exec.approval.request 注册时一致）
        tool_name: 工具名称
        assistant_id: 助手 ID
        resolution: 审批结果，"allow" 或 "deny"
        gateway_url: 可选的 Gateway URL。如果不提供，会根据 assistant_id 自动查询。
        gateway_token: 可选的 Gateway 认证 token。如果不提供，会根据 assistant_id 自动查询。
        is_allow_always: 是否为 "Always Allow" 决策。如果为 True，会传递 "allow-always"
            给 Gateway，使 IronAgent 也执行持久化白名单逻辑。

    Returns:
        是否解决成功
    """
    # 如果没有提供 gateway_url 或 gateway_token，尝试根据 assistant_id 查询
    if assistant_id and (not gateway_url or not gateway_token):
        resolved_url, resolved_token = await _get_gateway_url_for_assistant(assistant_id)
        gateway_url = gateway_url or resolved_url
        gateway_token = gateway_token or resolved_token

    client = get_gateway_client(gateway_url, gateway_token=gateway_token)

    # 将 IronAgent 的 resolution 映射为 IronAgent 的 decision
    # IronAgent 使用 "allow-once" / "allow-always" / "deny"
    # 当 is_allow_always=True 时，传递 "allow-always" 以便 IronAgent 执行持久化白名单
    if resolution == "allow":
        decision = "allow-always" if is_allow_always else "allow-once"
    else:
        decision = "deny"

    return await client.resolve_approval(approval_id, decision)


async def notify_approval_requested(
    approval_id: str,
    tool_name: str,
    assistant_id: str | None,
    reason: str,
    approval_scope: str,  # "user" or "admin"
    gateway_url: str | None = None,
    gateway_token: str | None = None,
    session_key: str | None = None,
    expires_at: datetime | None = None,
    admin_backend_url: str | None = None,
) -> bool:
    """
    通知 Gateway 有新的审批请求需要处理

    通过 Gateway 的 exec.approval.request 方法注册审批请求。
    这会在 Gateway 的 ExecApprovalManager 中创建一个 pending 条目，
    使得后续的 exec.approval.resolve 调用能够正确解除等待。

    Args:
        approval_id: 审批 ID（将作为 Gateway 审批的 ID）
        tool_name: 工具名称
        assistant_id: 助手 ID
        reason: 审批原因
        approval_scope: 审批范围，"user" 或 "admin"
        gateway_url: 可选的 Gateway URL。如果不提供，会根据 assistant_id 自动查询。
        gateway_token: 可选的 Gateway 认证 token。如果不提供，会根据 assistant_id 自动查询。
        session_key: 可选的 session key
        expires_at: 审批过期时间
        admin_backend_url: 管理后台 URL，用于前端展示审批链接

    Returns:
        是否注册成功
    """
    # 如果没有提供 gateway_url 或 gateway_token，尝试根据 assistant_id 查询
    if assistant_id and (not gateway_url or not gateway_token):
        resolved_url, resolved_token = await _get_gateway_url_for_assistant(assistant_id)
        gateway_url = gateway_url or resolved_url
        gateway_token = gateway_token or resolved_token

    client = get_gateway_client(gateway_url, gateway_token=gateway_token)

    # 构建符合 Gateway exec.approval.request 参数格式的 payload
    now = datetime.now(UTC)
    expires_at = expires_at or (now + timedelta(minutes=15))
    timeout_ms = int((expires_at - now).total_seconds() * 1000)

    params = {
        "id": approval_id,  # 使用 IronAgent 的 approval_id 作为 Gateway 审批 ID
        "command": tool_name,
        "sessionKey": session_key,
        "security": "allowlist",  # 使用 allowlist 安全模式
        "ask": approval_scope,
        "agentId": assistant_id or "",
        "timeoutMs": timeout_ms,
        "twoPhase": True,  # 两阶段注册，确保 ID 立即生效
    }

    # NOTE: adminBackendUrl 不能通过 Gateway exec.approval.request 参数传递
    # (Gateway schema 使用 additionalProperties: false)，前端需要另外获取此信息

    return await client._request_approval_via_gateway(params)
