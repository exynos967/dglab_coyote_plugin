import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from src.plugin_system import (
    BaseCommand,
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

    async def disconnect(self, reset_channels: bool = True) -> Tuple[bool, str]:
        """主动断连终端，并关闭内置 WS 服务端。"""
        async with self._lock:
            if self._client is None and self._server is None:
                return True, "当前没有活跃的郊狼连接。"

            cleanup_errors: List[str] = []

            if (
                reset_channels
                and self._client is not None
                and Channel is not None
                and StrengthOperationType is not None
            ):
                for channel_name, channel in (("A", Channel.A), ("B", Channel.B)):  # type: ignore[assignment]
                    try:
                        await self._client.set_strength(channel, StrengthOperationType.SET_TO, 0)  # type: ignore[arg-type]
                    except Exception as exc:  # noqa: BLE001
                        cleanup_errors.append(f"{channel_name} 通道降强度失败: {exc}")
                    try:
                        await self._client.clear_pulses(channel)  # type: ignore[arg-type]
                    except Exception as exc:  # noqa: BLE001
                        cleanup_errors.append(f"{channel_name} 通道清空波形失败: {exc}")

            await self._close_unlocked()

        if cleanup_errors:
            detail = "；".join(cleanup_errors)
            logger.warning(f"[Coyote] 断连时通道清理存在失败: {detail}")
            return True, f"已断开设备并关闭本地 WS 服务端，但部分通道清理失败：{detail}"
        return True, "已断开设备连接并关闭本地 WS 服务端。"

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


def resolve_server_uri(component: Any, server_uri_override: Any | None = None) -> Tuple[str | None, str | None]:
    """统一解析组件使用的 DG-Lab WebSocket 地址。"""
    override_uri = str(server_uri_override).strip() if server_uri_override is not None else ""
    explicit_server_uri = override_uri or str(component.get_config("connection.server_uri", "") or "").strip()

    try:
        return (
            build_server_uri_from_connection_config(
                explicit_server_uri=explicit_server_uri,
                server_scheme=str(component.get_config("connection.server_scheme", "ws") or "ws"),
                local_lan_ip=str(component.get_config("connection.local_lan_ip", "") or ""),
                server_port=component.get_config("connection.server_port", 5678),
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


class CoyoteConnectCommand(BaseCommand):
    """通过指令连接 DG-Lab 设备并返回二维码。"""

    command_name = "coyote_connect"
    command_description = "连接 DG-Lab 设备并返回扫码二维码"
    command_pattern = r"^/(?:coyote_connect|郊狼连接)(?:\s+(?P<bind_timeout>\d+(?:\.\d+)?))?$"

    async def execute(self) -> Tuple[bool, str | None, int]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws。"
            await self.send_text(msg)
            return False, msg, 2

        server_uri, error = resolve_server_uri(self)
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg, 2

        register_timeout = float(self.get_config("connection.register_timeout", 10.0))
        bind_timeout_raw = self.matched_groups.get("bind_timeout")
        bind_timeout = (
            float(bind_timeout_raw)
            if bind_timeout_raw
            else float(self.get_config("connection.bind_timeout", 60.0))
        )

        try:
            result = await _COYOTE_MANAGER.get_qrcode_and_maybe_bind(
                server_uri=server_uri,
                register_timeout=register_timeout,
                bind_timeout=bind_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Coyote] 指令连接 DG-Lab 服务端失败")
            msg = f"连接 DG-Lab 服务端失败: {exc}"
            await self.send_text(msg)
            return False, msg, 2

        qrcode_url = result.get("qrcode_url") or ""
        bind_desc = ""
        if result.get("bind_result") is not None:
            bind_desc = f"，绑定结果：{result['bind_result']}"

        content = (
            f"已连接 DG-Lab 服务端，二维码链接：{qrcode_url or '获取失败'}{bind_desc}。"
            "请使用 DG-Lab App 扫描二维码完成绑定。"
        )
        await self.send_text(content)
        return True, content, 2


class CoyoteDisconnectCommand(BaseCommand):
    """通过指令断连 DG-Lab 并关闭内置 WS 服务端。"""

    command_name = "coyote_disconnect"
    command_description = "断开 DG-Lab 设备连接并关闭本地 WS 服务端"
    command_pattern = r"^/(?:coyote_disconnect|郊狼断连)$"

    async def execute(self) -> Tuple[bool, str | None, int]:
        ok, message = await _COYOTE_MANAGER.disconnect(reset_channels=True)
        await self.send_text(message)
        return ok, message, 2


class CoyoteChannelOnCommand(BaseCommand):
    """通过指令开启指定通道输出。"""

    command_name = "coyote_channel_on"
    command_description = "开启郊狼通道输出（可选预设与强度）"
    command_pattern = (
        r"^/(?:coyote_channel_on|郊狼开启通道)\s+"
        r"(?P<channel>[AaBb])"
        r"(?:\s+(?P<preset>\S+))?"
        r"(?:\s+(?P<strength>\d{1,3}))?$"
    )

    async def execute(self) -> Tuple[bool, str | None, int]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws。"
            await self.send_text(msg)
            return False, msg, 2

        server_uri, error = resolve_server_uri(self)
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg, 2

        channel_name = str(self.matched_groups.get("channel", "")).upper()
        preset_name = str(
            self.matched_groups.get("preset")
            or self.get_config("control.command_default_preset", "steady")
            or "steady"
        ).lower()
        if preset_name not in PRESET_PULSES:
            msg = f"未知预设波形: {preset_name}，当前支持: {', '.join(PRESET_PULSES.keys())}"
            await self.send_text(msg)
            return False, msg, 2

        max_value = int(self.get_config("control.max_intensity", 200))
        strength_raw = self.matched_groups.get("strength")
        if strength_raw:
            strength_value = int(strength_raw)
        else:
            strength_value = int(self.get_config("control.command_default_strength", 30))
        strength_value = max(0, min(max_value, strength_value))

        register_timeout = float(self.get_config("connection.register_timeout", 10.0))
        bind_timeout = float(self.get_config("connection.bind_timeout", 60.0))
        heartbeat_interval = float(self.get_config("connection.heartbeat_interval", 20.0))

        ok_loop, msg_loop = await _COYOTE_MANAGER.start_waveform_loop(
            server_uri=server_uri,
            register_timeout=register_timeout,
            channel_name=channel_name,
            pulses=PRESET_PULSES[preset_name],
            bind_timeout=bind_timeout,
            heartbeat_interval=heartbeat_interval,
        )
        if not ok_loop:
            await self.send_text(msg_loop)
            return False, msg_loop, 2

        ok_strength, msg_strength = await _COYOTE_MANAGER.set_strength(
            server_uri=server_uri,
            register_timeout=register_timeout,
            channel_name=channel_name,
            mode_name="set",
            value=strength_value,
            max_value=max_value,
            bind_timeout=bind_timeout,
            heartbeat_interval=heartbeat_interval,
        )
        if not ok_strength:
            await _COYOTE_MANAGER.clear_pulses(
                server_uri=server_uri,
                register_timeout=register_timeout,
                channel_name=channel_name,
            )
            rollback_msg = f"开启通道失败，已回滚停止该通道波形。原因：{msg_strength}"
            await self.send_text(rollback_msg)
            return False, rollback_msg, 2

        content = f"已开启 {channel_name} 通道输出（预设: {preset_name}，强度: {strength_value}）。"
        await self.send_text(content)
        return True, content, 2


class CoyoteChannelOffCommand(BaseCommand):
    """通过指令关闭指定通道输出。"""

    command_name = "coyote_channel_off"
    command_description = "关闭郊狼通道输出并清空波形"
    command_pattern = r"^/(?:coyote_channel_off|郊狼关闭通道)\s+(?P<channel>[AaBb])$"

    async def execute(self) -> Tuple[bool, str | None, int]:
        if not _COYOTE_MANAGER.available:
            msg = "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws。"
            await self.send_text(msg)
            return False, msg, 2

        server_uri, error = resolve_server_uri(self)
        if not server_uri:
            msg = error or "未配置 DG-Lab WebSocket 服务端地址，请检查 connection 配置。"
            await self.send_text(msg)
            return False, msg, 2

        channel_name = str(self.matched_groups.get("channel", "")).upper()
        register_timeout = float(self.get_config("connection.register_timeout", 10.0))
        bind_timeout = float(self.get_config("connection.bind_timeout", 60.0))
        heartbeat_interval = float(self.get_config("connection.heartbeat_interval", 20.0))
        max_value = int(self.get_config("control.max_intensity", 200))

        ok_strength, msg_strength = await _COYOTE_MANAGER.set_strength(
            server_uri=server_uri,
            register_timeout=register_timeout,
            channel_name=channel_name,
            mode_name="set",
            value=0,
            max_value=max_value,
            bind_timeout=bind_timeout,
            heartbeat_interval=heartbeat_interval,
        )
        ok_clear, msg_clear = await _COYOTE_MANAGER.clear_pulses(
            server_uri=server_uri,
            register_timeout=register_timeout,
            channel_name=channel_name,
        )

        if ok_strength and ok_clear:
            content = f"已关闭 {channel_name} 通道输出（强度归零并清空波形）。"
            await self.send_text(content)
            return True, content, 2

        failed_parts: List[str] = []
        if not ok_strength:
            failed_parts.append(f"降强度失败: {msg_strength}")
        if not ok_clear:
            failed_parts.append(f"清空波形失败: {msg_clear}")
        content = f"关闭 {channel_name} 通道输出时部分失败：{'；'.join(failed_parts)}"
        await self.send_text(content)
        return False, content, 2


@register_plugin
class DGLabCoyotePlugin(BasePlugin):
    """DG-Lab 郊狼控制插件，通过 Command 指令控制连接与通道输出。"""

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
                default="1.3.0",
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
            "command_default_preset": ConfigField(
                type=str,
                default="steady",
                description="开启通道指令（/coyote_channel_on）默认使用的预设波形名称",
                example="steady",
            ),
            "command_default_strength": ConfigField(
                type=int,
                default=30,
                description="开启通道指令（/coyote_channel_on）默认强度",
                min=0,
                max=200,
                example="30",
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, type]]:
        return [
            (CoyoteConnectCommand.get_command_info(), CoyoteConnectCommand),
            (CoyoteDisconnectCommand.get_command_info(), CoyoteDisconnectCommand),
            (CoyoteChannelOnCommand.get_command_info(), CoyoteChannelOnCommand),
            (CoyoteChannelOffCommand.get_command_info(), CoyoteChannelOffCommand),
        ]

    def __init__(self, plugin_dir: str):
        super().__init__(plugin_dir)
        try:
            pulse_dir = self.get_config("connection.pulse_dir", "pulse_dir")
        except Exception:
            pulse_dir = "pulse_dir"
        load_pulse_presets_from_dir(Path(self.plugin_dir), str(pulse_dir))
