import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from src.plugin_system import (
    ActionActivationType,
    BaseAction,
    BasePlugin,
    ComponentInfo,
    ConfigField,
    PythonDependency,
    get_logger,
    register_plugin,
)

logger = get_logger("dglab_coyote_plugin")


# 尝试导入 pydglab_ws，如果环境中未安装则回退到插件内置版本
try:  # pragma: no cover - 运行环境由宿主提供
    from pydglab_ws import (  # type: ignore
        Channel,
        DGLabLocalClient,
        DGLabWSServer,
        RetCode,
        StrengthOperationType,
    )
except ImportError:  # pragma: no cover - 仅在缺少依赖时执行
    _CURRENT_DIR = Path(__file__).resolve().parent
    _LOCAL_LIB = _CURRENT_DIR.parent / "pydglab_ws-1.1.0"
    if _LOCAL_LIB.is_dir() and str(_LOCAL_LIB) not in sys.path:
        sys.path.insert(0, str(_LOCAL_LIB))
    try:
        from pydglab_ws import (  # type: ignore
            Channel,
            DGLabLocalClient,
            DGLabWSServer,
            RetCode,
            StrengthOperationType,
        )
    except ImportError:  # 最终仍然失败时，置为 None 交由工具层处理友好错误
        Channel = None  # type: ignore[assignment]
        DGLabLocalClient = None  # type: ignore[assignment]
        DGLabWSServer = None  # type: ignore[assignment]
        RetCode = None  # type: ignore[assignment]
        StrengthOperationType = None  # type: ignore[assignment]


class CoyoteConnectionManager:
    """郊狼（DG-Lab）连接与控制管理器

    负责维护到 DG-Lab WebSocket 服务端的长连接，并提供统一的强度与波形控制接口。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._server: Any | None = None
        self._client: Any | None = None
        self._server_uri: str | None = None
        self._heartbeat_interval: float | None = None
        self._monitor_task: asyncio.Task | None = None
        self._last_bound: bool = False
        self._waveform_tasks: Dict[str, asyncio.Task] = {}

    @property
    def available(self) -> bool:
        """当前环境是否可以使用 pydglab_ws。"""
        return DGLabWSServer is not None and DGLabLocalClient is not None

    async def _close_unlocked(self) -> None:
        """关闭现有服务端和终端（在持有锁的前提下调用）。"""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
        for task in self._waveform_tasks.values():
            task.cancel()
        self._waveform_tasks.clear()
        if self._server is not None:
            try:
                await self._server.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Coyote] 关闭内置 DG-Lab 服务端失败: {exc}")
        self._server = None
        self._client = None
        self._server_uri = None
        self._heartbeat_interval = None
        self._last_bound = False

    async def get_or_create_client(
        self,
        server_uri: str,
        register_timeout: float | None,
        heartbeat_interval: float | None,
    ) -> Any:
        """获取或创建 DGLabLocalClient 实例，并在需要时启动内置 DGLabWSServer。

        注意：此处的 register_timeout 参数在内置服务端模式下不会使用，仅为兼容保留。
        """
        if not self.available:
            raise RuntimeError("pydglab_ws 未安装或导入失败，无法连接郊狼设备")

        parsed = urlparse(server_uri)
        scheme = parsed.scheme or "ws"
        host = parsed.hostname
        port = parsed.port
        if host is None or port is None:
            raise ValueError(f"无效的 DG-Lab WebSocket 地址: {server_uri}")
        if scheme not in ("ws", "wss"):
            raise ValueError(f"不支持的协议: {scheme}，仅支持 ws / wss")

        base_uri = f"{scheme}://{host}:{port}"
        hb = float(heartbeat_interval) if heartbeat_interval is not None else 20.0

        async with self._lock:
            if self._client is not None and self._server_uri == base_uri:
                return self._client

            await self._close_unlocked()

            logger.info(f"[Coyote] 启动内置 DG-Lab WebSocket 服务端: {base_uri}")
            try:
                server = DGLabWSServer(host, port, hb)  # type: ignore[call-arg]
                await server.__aenter__()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[Coyote] 启动 DG-Lab 内置服务端失败")
                raise RuntimeError(f"启动 DG-Lab 内置服务端失败: {exc}") from exc

            client = server.new_local_client()  # type: ignore[assignment]

            self._server = server
            self._client = client
            self._server_uri = base_uri
            self._heartbeat_interval = hb

            # 启动简单的监控任务，持续读取心跳/断开事件，避免消息队列堆积
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_client(client))
            return client

    async def _monitor_client(self, client: Any) -> None:
        """后台监控终端连接状态，用于长时间连接的保活与日志记录。"""
        if RetCode is None:
            return
        try:
            async for data in client.data_generator():  # type: ignore[attr-defined]
                if isinstance(data, RetCode):
                    if data.name == "CLIENT_DISCONNECTED":
                        logger.info("[Coyote] App 断开连接（CLIENT_DISCONNECTED）")
                        self._last_bound = False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Coyote] 连接监控任务结束: {exc}")

    async def get_status(self) -> Dict[str, Any]:
        """返回当前连接状态，用于调试和工具返回。"""
        async with self._lock:
            client = self._client
            if client is None:
                return {"connected": False, "bound": False}

            return {
                "connected": True,
                "bound": not getattr(client, "not_bind", True),
                "client_id": str(getattr(client, "client_id", "") or ""),
                "target_id": str(getattr(client, "target_id", "") or ""),
            }

    async def get_qrcode_and_maybe_bind(
        self,
        server_uri: str,
        register_timeout: float | None,
        bind_timeout: float | None,
    ) -> Dict[str, Any]:
        """创建/复用连接，获取二维码链接，并可选等待绑定完成。"""
        # 连接时总是使用当前配置中的心跳设置
        client = await self.get_or_create_client(
            server_uri,
            register_timeout,
            heartbeat_interval=None,
        )
        # 对于本地终端，需要显式传入服务端 URI
        qrcode_url = client.get_qrcode(self._server_uri)  # type: ignore[arg-type]

        bind_success = False
        bind_result: str | None = None

        if bind_timeout and bind_timeout > 0:
            try:
                ret = await asyncio.wait_for(client.bind(), bind_timeout)  # type: ignore[arg-type]
                if RetCode is not None and isinstance(ret, RetCode):
                    bind_success = ret == RetCode.SUCCESS  # type: ignore[comparison-overlap]
                    bind_result = ret.name
                else:
                    bind_success = True
                    bind_result = str(ret)
            except asyncio.TimeoutError:
                bind_success = False
                bind_result = "TIMEOUT"
            except Exception as exc:  # noqa: BLE001
                bind_success = False
                bind_result = f"ERROR:{exc}"

        status = await self.get_status()
        return {
            "qrcode_url": qrcode_url,
            "bind_success": bind_success,
            "bind_result": bind_result,
            "status": status,
        }

    async def ensure_ready_for_control(
        self,
        server_uri: str,
        register_timeout: float | None,
        bind_timeout: float | None,
        heartbeat_interval: float | None,
    ) -> Tuple[bool, str, Any | None]:
        """用于控制前的通用检查：依赖、连接，并在必要时等待绑定完成。"""
        if not self.available:
            return False, "pydglab_ws 未安装或导入失败", None

        client = await self.get_or_create_client(server_uri, register_timeout, heartbeat_interval)

        # 尝试确保已绑定，如有配置则增加超时保护
        try:
            if bind_timeout and bind_timeout > 0:
                await asyncio.wait_for(client.ensure_bind(), bind_timeout)  # type: ignore[attr-defined]
            else:
                await client.ensure_bind()  # type: ignore[attr-defined]
        except asyncio.TimeoutError:
            return (
                False,
                f"在 {bind_timeout} 秒内未完成与 App 的绑定，请确认已使用二维码扫码连接",
                None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Coyote] 等待绑定时出错")
            return False, f"等待与 App 绑定失败: {exc}", None

        # 绑定成功后输出一次日志
        if not getattr(client, "not_bind", True) and not self._last_bound:
            logger.info("[Coyote] App 绑定成功（ensure_bind 完成）")
            self._last_bound = True

        return True, "", client

    async def set_strength(
        self,
        server_uri: str,
        register_timeout: float | None,
        channel_name: str,
        mode_name: str,
        value: int,
        max_value: int,
        bind_timeout: float | None,
        heartbeat_interval: float | None,
    ) -> Tuple[bool, str]:
        """设置 A/B 通道强度。"""
        ok, message, client = await self.ensure_ready_for_control(
            server_uri,
            register_timeout,
            bind_timeout,
            heartbeat_interval,
        )
        if not ok or client is None:
            return False, message

        if Channel is None or StrengthOperationType is None:
            return False, "pydglab_ws 枚举未正确导入"

        channel_name = channel_name.upper()
        if channel_name == "A":
            channel = Channel.A  # type: ignore[assignment]
        elif channel_name == "B":
            channel = Channel.B  # type: ignore[assignment]
        else:
            return False, f"无效通道: {channel_name}，仅支持 A 或 B"

        mode_map = {
            "set": StrengthOperationType.SET_TO,
            "set_to": StrengthOperationType.SET_TO,
            "increase": StrengthOperationType.INCREASE,
            "decrease": StrengthOperationType.DECREASE,
        }
        op_key = mode_name.lower()
        if op_key not in mode_map:
            return False, f"无效模式: {mode_name}，仅支持 set/increase/decrease"
        op = mode_map[op_key]  # type: ignore[assignment]

        max_value = max(0, min(200, int(max_value)))
        value = int(value)
        if op == StrengthOperationType.SET_TO and value > max_value:  # type: ignore[comparison-overlap]
            value = max_value

        if value < 0:
            value = 0
        if value > 200:
            value = 200

        try:
            await client.set_strength(channel, op, value)  # type: ignore[arg-type]
            logger.info(
                f"[Coyote] 设置强度成功: channel={channel_name}, "
                f"mode={getattr(op, 'name', str(op))}, value={value}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Coyote] 设置强度失败")
            return False, f"设置强度失败: {exc}"

        return True, f"已设置 {channel_name} 通道，模式={op.name}，值={value}"

    async def add_pulses(
        self,
        server_uri: str,
        register_timeout: float | None,
        channel_name: str,
        pulses: List[Tuple[List[int], List[int]]],
    ) -> Tuple[bool, str]:
        """下发波形数据。"""
        ok, message, client = await self.ensure_ready_for_control(
            server_uri,
            register_timeout,
            bind_timeout=None,
            heartbeat_interval=None,
        )
        if not ok or client is None:
            return False, message

        if Channel is None:
            return False, "pydglab_ws 枚举未正确导入"

        channel_name = channel_name.upper()
        if channel_name == "A":
            channel = Channel.A  # type: ignore[assignment]
        elif channel_name == "B":
            channel = Channel.B  # type: ignore[assignment]
        else:
            return False, f"无效通道: {channel_name}，仅支持 A 或 B"

        pulse_ops: List[tuple] = []
        for index, (freqs, strengths) in enumerate(pulses, start=1):
            if len(freqs) != 4 or len(strengths) != 4:
                return False, f"第 {index} 组波形数据不合法，需要 4 个频率和 4 个强度"
            try:
                freq_tuple = tuple(int(v) for v in freqs)
                strength_tuple = tuple(int(v) for v in strengths)
            except ValueError:
                return False, f"第 {index} 组波形数据包含无法转换为整数的值"
            pulse_ops.append((freq_tuple, strength_tuple))

        try:
            await client.add_pulses(channel, *pulse_ops)  # type: ignore[arg-type]
            logger.info(
                f"[Coyote] 下发波形成功: channel={channel_name}, "
                f"pulses_count={len(pulse_ops)}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Coyote] 下发波形数据失败")
            return False, f"下发波形数据失败: {exc}"

        return True, f"已向 {channel_name} 通道追加 {len(pulse_ops)} 组波形数据"

    async def start_waveform_loop(
        self,
        server_uri: str,
        register_timeout: float | None,
        channel_name: str,
        pulses: List[Tuple[List[int], List[int]]],
        bind_timeout: float | None,
        heartbeat_interval: float | None,
    ) -> Tuple[bool, str]:
        """启动指定通道的波形循环，下发相同波形以保持持续输出。"""
        if Channel is None:
            return False, "pydglab_ws 枚举未正确导入"

        channel_name = channel_name.upper()
        if channel_name not in ("A", "B"):
            return False, f"无效通道: {channel_name}，仅支持 A 或 B"

        # 先做一次立即下发，确保有数据
        ok, message = await self.add_pulses(
            server_uri=server_uri,
            register_timeout=register_timeout,
            channel_name=channel_name,
            pulses=pulses,
        )
        if not ok:
            return False, message

        # 取消该通道已有的循环任务
        existing = self._waveform_tasks.get(channel_name)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _loop() -> None:
            # 估算每次波形持续时间：每条 pulse 约 100ms
            interval = 0.1 * max(1, len(pulses))
            sleep_interval = max(0.1, interval * 0.8)
            while True:
                ok_inner, msg_inner, client = await self.ensure_ready_for_control(
                    server_uri=server_uri,
                    register_timeout=register_timeout,
                    bind_timeout=bind_timeout,
                    heartbeat_interval=heartbeat_interval,
                )
                if not ok_inner or client is None:
                    logger.warning(f"[Coyote] 波形循环中断: {msg_inner}")
                    break
                try:
                    pulse_ops: List[tuple] = []
                    for freqs, strengths in pulses:
                        freq_tuple = tuple(int(v) for v in freqs)
                        strength_tuple = tuple(int(v) for v in strengths)
                        pulse_ops.append((freq_tuple, strength_tuple))
                    channel = Channel.A if channel_name == "A" else Channel.B  # type: ignore[assignment]
                    await client.add_pulses(channel, *pulse_ops)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    logger.exception("[Coyote] 波形循环下发失败")
                    break
                await asyncio.sleep(sleep_interval)

        self._waveform_tasks[channel_name] = asyncio.create_task(_loop())
        logger.info(
            f"[Coyote] 启动波形循环: channel={channel_name}, pulses_count={len(pulses)}"
        )
        return True, f"已启动 {channel_name} 通道的波形循环"

    async def clear_pulses(
        self,
        server_uri: str,
        register_timeout: float | None,
        channel_name: str,
    ) -> Tuple[bool, str]:
        """清空指定通道的波形队列。"""
        ok, message, client = await self.ensure_ready_for_control(
            server_uri,
            register_timeout,
            bind_timeout=None,
            heartbeat_interval=None,
        )
        if not ok or client is None:
            return False, message

        if Channel is None:
            return False, "pydglab_ws 枚举未正确导入"

        channel_name = channel_name.upper()
        if channel_name == "A":
            channel = Channel.A  # type: ignore[assignment]
        elif channel_name == "B":
            channel = Channel.B  # type: ignore[assignment]
        else:
            return False, f"无效通道: {channel_name}，仅支持 A 或 B"

        try:
            await client.clear_pulses(channel)  # type: ignore[arg-type]
            logger.info(f"[Coyote] 清空波形队列成功: channel={channel_name}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Coyote] 清空波形队列失败")
            return False, f"清空波形队列失败: {exc}"

        # 同时停止该通道的循环任务
        task = self._waveform_tasks.get(channel_name)
        if task is not None and not task.done():
            task.cancel()
            self._waveform_tasks.pop(channel_name, None)

        return True, f"已清空 {channel_name} 通道的波形队列"


_COYOTE_MANAGER = CoyoteConnectionManager()


# 内置波形预设：频率范围 [10, 240]，强度范围 [0, 100]
PRESET_PULSES: Dict[str, List[Tuple[List[int], List[int]]]] = {
    # 稳定连续输出，适合测试是否有电
    "steady": [
        ([80, 80, 80, 80], [80, 80, 80, 80]),
    ],
    # 有起伏的脉冲感
    "pulse": [
        ([120, 60, 120, 60], [90, 30, 90, 30]),
    ],
    # 类似波浪的强度变化
    "wave": [
        ([40, 80, 40, 80], [30, 70, 30, 70]),
    ],
}


def build_server_uri_from_connection_config(
    *,
    explicit_server_uri: str | None,
    server_scheme: str,
    local_lan_ip: str,
    server_port: Any,
) -> str:
    """按配置拼装 DG-Lab WebSocket 地址。"""
    explicit_uri = (explicit_server_uri or "").strip()
    if explicit_uri:
        return explicit_uri

    host = (local_lan_ip or "").strip()
    if not host:
        raise ValueError("未配置 connection.local_lan_ip，请填写运行插件机器的局域网 IP。")

    scheme = (server_scheme or "ws").strip().lower()
    if scheme not in {"ws", "wss"}:
        raise ValueError(f"connection.server_scheme 配置无效: {scheme}，仅支持 ws 或 wss。")

    try:
        port = int(server_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("connection.server_port 必须是 1-65535 的整数。") from exc

    if not 1 <= port <= 65535:
        raise ValueError("connection.server_port 必须在 1-65535 范围内。")

    return f"{scheme}://{host}:{port}"


def resolve_server_uri(action: BaseAction, server_uri_override: Any | None = None) -> Tuple[str | None, str | None]:
    """统一解析 Action 使用的 DG-Lab WebSocket 地址。"""
    override_uri = str(server_uri_override).strip() if server_uri_override is not None else ""
    explicit_server_uri = override_uri or str(action.get_config("connection.server_uri", "") or "").strip()

    try:
        return (
            build_server_uri_from_connection_config(
                explicit_server_uri=explicit_server_uri,
                server_scheme=str(action.get_config("connection.server_scheme", "ws") or "ws"),
                local_lan_ip=str(action.get_config("connection.local_lan_ip", "") or ""),
                server_port=action.get_config("connection.server_port", 5678),
            ),
            None,
        )
    except ValueError as exc:
        return None, str(exc)


def parse_dungeonlab_pulse(content: str) -> List[Tuple[List[int], List[int]]]:
    """从 DungeonLab 导出的 .pulse 文本解析出近似的波形数据。

    说明：.pulse 原始格式较复杂，这里采用近似方案：
    - 仅解析每个 section 中斜杠 `/` 后的强度点，如 `100.00-1`
    - 将数值部分视为 0-100 的强度百分比，忽略后面的标志位
    - 每 4 个点组成一次 PulseOperation，使用固定频率 80
    """
    if not content.startswith("Dungeonlab+pulse"):
        return []

    try:
        _, rest = content.split(":", 1)
    except ValueError:
        return []

    segments = rest.split("+section+")
    strengths_all: List[float] = []
    for seg in segments:
        if "/" not in seg:
            continue
        _, data = seg.split("/", 1)
        tokens = [t for t in data.split(",") if t]
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            # 形如 "100.00-1"
            try:
                val_str = token.split("-", 1)[0]
                v = float(val_str)
                strengths_all.append(v)
            except ValueError:
                continue

    pulses: List[Tuple[List[int], List[int]]] = []
    if not strengths_all:
        return pulses

    # 将强度序列按 4 个一组组装 PulseOperation
    i = 0
    while i < len(strengths_all):
        group = strengths_all[i : i + 4]
        if len(group) < 4:
            group += [group[-1]] * (4 - len(group))
        strengths = [max(0, min(100, int(round(v)))) for v in group]
        freqs = [80, 80, 80, 80]
        pulses.append((freqs, strengths))
        i += 4

    # 防止超出 pydglab_ws 限制，这里做一个保守截断（每次 add_pulses 上限为 86 组）
    return pulses[:80]


def load_pulse_presets_from_dir(base_dir: Path, subdir: str) -> None:
    """从插件目录下的指定子目录加载 .pulse 文件为预设波形。"""
    global PRESET_PULSES
    dir_path = (base_dir / subdir).resolve()
    if not dir_path.is_dir():
        return

    for file in dir_path.glob("*.pulse"):
        try:
            text = file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        pulses = parse_dungeonlab_pulse(text)
        if not pulses:
            continue
        preset_name = file.stem  # 例如 "128-星儿"
        PRESET_PULSES[preset_name] = pulses


class CoyoteConnectAction(BaseAction):
    """建立与 DG-Lab App 的连接并获取二维码。"""

    action_name = "coyote_connect"
    action_description = "连接 DG-Lab WebSocket 服务端，返回用于 App 扫码绑定的二维码链接"
    activation_type = ActionActivationType.ALWAYS
    associated_types = ["text"]
    parallel_action = False

    action_parameters = {
        "server_uri": "可选：覆盖配置项解析出的 DG-Lab WebSocket 地址，例如 ws://192.168.1.23:5678。",
        "register_timeout": "可选：终端注册超时时间（秒），默认使用配置 connection.register_timeout。",
        "bind_timeout": "可选：等待 App 扫码绑定的超时时间（秒），<=0 表示不等待，默认使用配置 connection.bind_timeout。",
    }

    action_require = [
        "当用户明确要求连接或重新连接郊狼 / DG-Lab 设备，或需要二维码进行绑定时使用本 Action。",
        "当控制强度或波形时提示未绑定 App 时，可以先调用本 Action 引导用户扫码绑定。",
        "如果当前会话已经成功绑定且连接稳定，不要频繁重复调用，除非用户要求重连或更换设备。",
    ]

    async def execute(self) -> Tuple[bool, str]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用。"
            await self.send_text(msg)
            return False, msg

        data = self.action_data or {}
        server_uri, error = resolve_server_uri(self, data.get("server_uri"))
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg

        register_timeout = data.get("register_timeout", self.get_config("connection.register_timeout", 10.0))
        bind_timeout = data.get("bind_timeout", self.get_config("connection.bind_timeout", 60.0))

        try:
            result = await _COYOTE_MANAGER.get_qrcode_and_maybe_bind(
                server_uri=server_uri,
                register_timeout=float(register_timeout) if register_timeout is not None else None,
                bind_timeout=float(bind_timeout) if bind_timeout is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Coyote] 连接 DG-Lab 服务端失败")
            msg = f"连接 DG-Lab 服务端失败: {exc}"
            await self.send_text(msg)
            return False, msg

        qrcode_url = result.get("qrcode_url") or ""
        bind_desc = ""
        if result.get("bind_result") is not None:
            bind_desc = f"，绑定结果：{result['bind_result']}"

        content = (
            f"已连接 DG-Lab 服务端，二维码链接：{qrcode_url or '获取失败'}{bind_desc}。"
            "请使用 DG-Lab App 扫描二维码完成绑定。"
        )
        await self.send_text(content)
        return True, content


class CoyoteSetStrengthAction(BaseAction):
    """控制 DG-Lab A/B 通道强度。"""

    action_name = "coyote_set_strength"
    action_description = "设置郊狼 A/B 通道的强度，可选择设定绝对值或相对增减"
    activation_type = ActionActivationType.ALWAYS
    associated_types = ["text"]
    parallel_action = False

    action_parameters = {
        "channel": "目标通道，仅支持 'A' 或 'B'。",
        "mode": "强度模式：set=设定绝对值，increase=相对增加，decrease=相对减少。",
        "value": "强度数值，建议在 0-200 之间；对于 increase/decrease 表示增量。",
    }

    action_require = [
        "当用户明确要求调高、调低或设置郊狼某个通道的强度时使用本 Action。",
        "首次为当前会话控制强度时，应从较低强度开始（例如 10~30），在用户确认可以接受后再逐步提高。",
        "当用户表示不适或要求停止时，应优先将强度设为 0 并提示已关闭输出。",
        "避免在短时间内多次将强度从很低直接设置到接近上限，除非用户明确要求且确认安全。",
    ]

    async def execute(self) -> Tuple[bool, str]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用。"
            await self.send_text(msg)
            return False, msg

        server_uri, error = resolve_server_uri(self)
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg

        data = self.action_data or {}
        channel = str(data.get("channel", "")).strip()
        mode = str(data.get("mode", "")).strip()
        try:
            value = int(data.get("value"))
        except (TypeError, ValueError):
            msg = "参数 value 无法转换为整数。"
            await self.send_text(msg)
            return False, msg

        max_value = int(self.get_config("control.max_intensity", 200))
        bind_timeout = float(self.get_config("connection.bind_timeout", 60.0))
        heartbeat_interval = float(self.get_config("connection.heartbeat_interval", 20.0))

        ok, message = await _COYOTE_MANAGER.set_strength(
            server_uri=server_uri,
            register_timeout=float(self.get_config("connection.register_timeout", 10.0)),
            channel_name=channel,
            mode_name=mode,
            value=value,
            max_value=max_value,
            bind_timeout=bind_timeout,
            heartbeat_interval=heartbeat_interval,
        )

        await self.send_text(message)
        return ok, message


class CoyoteAddWaveformAction(BaseAction):
    """下发 DG-Lab 波形数据。"""

    action_name = "coyote_add_waveform"
    action_description = "向郊狼设备追加波形数据，控制 A/B 通道的波形输出"
    activation_type = ActionActivationType.ALWAYS
    associated_types = ["text"]
    parallel_action = False

    action_parameters = {
        "channel": "目标通道，仅支持 'A' 或 'B'。",
        "pulses_json": (
            "波形数据 JSON 字符串，格式示例："
            '[{\"frequency\":[10,20,30,40],\"strength\":[10,20,30,40]}]，'
            "frequency 为 4 个频率，strength 为 4 个强度。"
        ),
    }

    action_require = [
        "当需要根据自定义 JSON 波形数据精细控制输出时使用本 Action。",
        "一般用户只需要使用预设波形，除非用户准备好了明确的频率/强度数组。",
    ]

    async def execute(self) -> Tuple[bool, str]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用。"
            await self.send_text(msg)
            return False, msg

        server_uri, error = resolve_server_uri(self)
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg

        data = self.action_data or {}
        channel = str(data.get("channel", "")).strip()
        pulses_raw = data.get("pulses_json")
        if not pulses_raw:
            msg = "缺少参数 pulses_json。"
            await self.send_text(msg)
            return False, msg

        try:
            pulses_data = json.loads(pulses_raw)
        except json.JSONDecodeError as exc:
            msg = f"解析波形 JSON 失败: {exc}"
            await self.send_text(msg)
            return False, msg

        if isinstance(pulses_data, dict):
            pulses_list = [pulses_data]
        elif isinstance(pulses_data, list):
            pulses_list = pulses_data
        else:
            msg = "pulses_json 必须是对象或对象数组。"
            await self.send_text(msg)
            return False, msg

        parsed_pulses: List[Tuple[List[int], List[int]]] = []
        for index, pulse in enumerate(pulses_list, start=1):
            if not isinstance(pulse, dict):
                msg = f"第 {index} 个波形数据不是对象。"
                await self.send_text(msg)
                return False, msg
            freqs = pulse.get("frequency") or pulse.get("freq")
            strengths = pulse.get("strength") or pulse.get("amplitude")
            if not isinstance(freqs, list) or not isinstance(strengths, list):
                msg = f"第 {index} 个波形数据的 frequency/strength 不是数组。"
                await self.send_text(msg)
                return False, msg
            parsed_pulses.append((freqs, strengths))

        ok, message = await _COYOTE_MANAGER.add_pulses(
            server_uri=server_uri,
            register_timeout=float(self.get_config("connection.register_timeout", 10.0)),
            channel_name=channel,
            pulses=parsed_pulses,
        )

        await self.send_text(message)
        return ok, message


class CoyoteClearWaveformAction(BaseAction):
    """清空指定通道的波形队列。"""

    action_name = "coyote_clear_waveform"
    action_description = "清空郊狼设备某个通道的波形队列，并停止该通道的循环任务"
    activation_type = ActionActivationType.ALWAYS
    associated_types = ["text"]
    parallel_action = False

    action_parameters = {
        "channel": "目标通道，仅支持 'A' 或 'B'。",
    }

    action_require = [
        "当用户希望停止某个通道的输出、切换波形、或结束本轮玩法时使用本 Action。",
        "在需要重置波形队列或准备切换到完全不同的预设前，也可以先调用本 Action。",
    ]

    async def execute(self) -> Tuple[bool, str]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用。"
            await self.send_text(msg)
            return False, msg

        server_uri, error = resolve_server_uri(self)
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg

        data = self.action_data or {}
        channel = str(data.get("channel", "")).strip()
        ok, message = await _COYOTE_MANAGER.clear_pulses(
            server_uri=server_uri,
            register_timeout=float(self.get_config("connection.register_timeout", 10.0)),
            channel_name=channel,
        )

        await self.send_text(message)
        return ok, message


class CoyotePlayPresetAction(BaseAction):
    """在指定通道播放内置或 .pulse 预设波形。"""

    action_name = "coyote_play_preset"
    action_description = "在指定通道播放预设好的郊狼波形（steady/pulse/wave 或 .pulse 文件预设），并以循环模式持续输出"
    activation_type = ActionActivationType.ALWAYS
    associated_types = ["text"]
    parallel_action = False

    action_parameters = {
        "channel": "目标通道，仅支持 'A' 或 'B'。",
        "preset": "预设波形名称，可选：steady（稳定）、pulse（脉冲）、wave（波浪），或 pulse_dir 目录下的 .pulse 文件名（不含扩展名）。",
    }

    action_require = [
        "当用户希望让郊狼持续输出某种预设波形时使用本 Action，例如『使用星儿预设』『切换到脉冲模式』『让 A 通道持续输出』。",
        "如果当前已经有波形循环在运行，切换预设时可以直接调用本 Action，新预设会自动覆盖旧的循环任务。",
        "对于只想测试是否有电的场景，可以选择较温和的预设并配合较低强度。",
    ]

    async def execute(self) -> Tuple[bool, str]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用。"
            await self.send_text(msg)
            return False, msg

        server_uri, error = resolve_server_uri(self)
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg

        data = self.action_data or {}
        channel = str(data.get("channel", "")).strip()
        preset_raw = str(data.get("preset", "")).strip().lower()
        if preset_raw not in PRESET_PULSES:
            msg = f"未知预设波形: {preset_raw}，当前支持: {', '.join(PRESET_PULSES.keys())}"
            await self.send_text(msg)
            return False, msg

        pulses = PRESET_PULSES[preset_raw]

        ok, message = await _COYOTE_MANAGER.start_waveform_loop(
            server_uri=server_uri,
            register_timeout=float(self.get_config("connection.register_timeout", 10.0)),
            channel_name=channel,
            pulses=pulses,
            bind_timeout=float(self.get_config("connection.bind_timeout", 60.0)),
            heartbeat_interval=float(self.get_config("connection.heartbeat_interval", 20.0)),
        )

        if ok:
            message = f"{message}（预设: {preset_raw}，循环模式）"

        await self.send_text(message)
        return ok, message


@register_plugin
class DGLabCoyotePlugin(BasePlugin):
    """DG-Lab 郊狼控制插件，通过 Action 调用控制强度和波形。"""

    plugin_name: str = "dglab_coyote_plugin"
    enable_plugin: bool = False
    dependencies: List[str] = []
    python_dependencies: List[PythonDependency] = [
        PythonDependency(
            package_name="pydglab_ws",
            install_name="pydglab-ws",
            version=">=1.1.0",
            optional=False,
            description="DG-Lab 郊狼 WebSocket 控制库",
        )
    ]
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息与启用开关",
        "connection": "DG-Lab WebSocket 连接配置",
        "control": "强度与波形控制安全设置",
    }

    config_schema: Dict[str, Dict[str, ConfigField]] = {
        "plugin": {
            "config_version": ConfigField(
                type=str,
                default="1.1.0",
                description="配置文件版本",
            ),
            "enabled": ConfigField(
                type=bool,
                default=False,
                description="是否启用郊狼控制插件",
            ),
        },
        "connection": {
            "local_lan_ip": ConfigField(
                type=str,
                default="127.0.0.1",
                description="运行插件机器的局域网 IP（建议必填）",
                required=True,
                placeholder="例如：127.0.0.1 或 192.168.1.23",
                hint="默认 127.0.0.1（本机回环）。服务器部署时请改为手机可访问到的局域网 IP。",
                pattern=r"^(?:\d{1,3}\.){3}\d{1,3}$",
                example="127.0.0.1",
                order=0,
            ),
            "server_port": ConfigField(
                type=int,
                default=5678,
                description="DG-Lab WebSocket 端口",
                min=1,
                max=65535,
                example="5678",
                order=1,
            ),
            "server_scheme": ConfigField(
                type=str,
                default="ws",
                description="DG-Lab WebSocket 协议",
                choices=["ws", "wss"],
                example="ws",
                order=2,
            ),
            "server_uri": ConfigField(
                type=str,
                default="",
                description="可选：完整 DG-Lab WebSocket 地址（填写后优先于 local_lan_ip + server_port + server_scheme）",
                placeholder="例如：ws://192.168.1.23:5678",
                hint="仅在需要自定义完整地址时填写；常规场景建议填写 local_lan_ip。",
                example="ws://192.168.1.23:5678",
                order=3,
            ),
            "register_timeout": ConfigField(
                type=float,
                default=10.0,
                description="终端注册超时时间（秒）",
                min=1.0,
                max=60.0,
                step=1.0,
                order=4,
            ),
            "bind_timeout": ConfigField(
                type=float,
                default=60.0,
                description="等待 App 扫码完成绑定的时间（秒），用于连接与控制工具",
                min=5.0,
                max=600.0,
                step=5.0,
                order=5,
            ),
            "heartbeat_interval": ConfigField(
                type=float,
                default=20.0,
                description="内置 DG-Lab WebSocket 服务端心跳间隔（秒），数值越小保活越积极",
                min=5.0,
                max=120.0,
                step=1.0,
                order=6,
            ),
            "pulse_dir": ConfigField(
                type=str,
                default="pulse_dir",
                description="存放 DungeonLab 导出 .pulse 文件的子目录（相对于插件目录），默认 dglab_coyote_plugin/pulse_dir",
                order=7,
            ),
        },
        "control": {
            "max_intensity": ConfigField(
                type=int,
                default=200,
                description="强度上限（0-200），set 模式会自动将值裁剪到此上限",
                min=0,
                max=200,
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, type]]:
        return [
            (CoyoteConnectAction.get_action_info(), CoyoteConnectAction),
            (CoyoteSetStrengthAction.get_action_info(), CoyoteSetStrengthAction),
            (CoyoteAddWaveformAction.get_action_info(), CoyoteAddWaveformAction),
            (CoyoteClearWaveformAction.get_action_info(), CoyoteClearWaveformAction),
            (CoyotePlayPresetAction.get_action_info(), CoyotePlayPresetAction),
        ]

    def __init__(self, plugin_dir: str):
        super().__init__(plugin_dir)
        try:
            pulse_dir = self.get_config("connection.pulse_dir", "pulse_dir")
        except Exception:
            pulse_dir = "pulse_dir"
        load_pulse_presets_from_dir(Path(self.plugin_dir), str(pulse_dir))
