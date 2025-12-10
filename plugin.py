import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.plugin_system import (
    BasePlugin,
    BaseTool,
    ComponentInfo,
    ConfigField,
    PythonDependency,
    ToolParamType,
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
                except Exception as exc:  # noqa: BLE001
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


class CoyoteConnectTool(BaseTool):
    """建立与 DG-Lab App 的连接并获取二维码。"""

    name = "coyote_connect"
    description = "连接 DG-Lab WebSocket 服务端，返回用于 App 扫码绑定的二维码链接"
    parameters = [
        (
            "server_uri",
            ToolParamType.STRING,
            "可选：DG-Lab WebSocket 服务端地址，如 ws://127.0.0.1:5678，默认使用插件配置 connection.server_uri",
            False,
            None,
        ),
        (
            "register_timeout",
            ToolParamType.FLOAT,
            "可选：终端注册超时时间（秒），默认使用插件配置 connection.register_timeout",
            False,
            None,
        ),
        (
            "bind_timeout",
            ToolParamType.FLOAT,
            "可选：等待 App 扫码绑定的超时时间（秒），<=0 表示不等待，默认使用插件配置 connection.bind_timeout",
            False,
            None,
        ),
    ]
    available_for_llm = True

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        if not _COYOTE_MANAGER.available:
            return {
                "name": self.name,
                "content": "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用",
                "ok": False,
            }

        server_uri_arg = function_args.get("server_uri")
        server_uri_cfg = self.get_config("connection.server_uri", "")
        server_uri = (server_uri_arg or server_uri_cfg or "").strip()
        if not server_uri:
            return {
                "name": self.name,
                "content": "未配置 DG-Lab WebSocket 服务端地址，请在插件配置 connection.server_uri 中设置或通过工具参数传入",
                "ok": False,
            }

        register_timeout = function_args.get(
            "register_timeout",
            self.get_config("connection.register_timeout", 10.0),
        )
        bind_timeout = function_args.get(
            "bind_timeout",
            self.get_config("connection.bind_timeout", 60.0),
        )

        try:
            result = await _COYOTE_MANAGER.get_qrcode_and_maybe_bind(
                server_uri=server_uri,
                register_timeout=float(register_timeout) if register_timeout is not None else None,
                bind_timeout=float(bind_timeout) if bind_timeout is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Coyote] 连接 DG-Lab 服务端失败")
            return {
                "name": self.name,
                "content": f"连接 DG-Lab 服务端失败: {exc}",
                "ok": False,
            }

        qrcode_url = result.get("qrcode_url") or ""
        bind_desc = ""
        if result.get("bind_result") is not None:
            bind_desc = f"，绑定结果：{result['bind_result']}"

        content = (
            f"已连接 DG-Lab 服务端，二维码链接：{qrcode_url or '获取失败'}{bind_desc}。"
            "请使用 DG-Lab App 扫描二维码完成绑定。"
        )

        return {
            "name": self.name,
            "content": content,
            "ok": True,
            "qrcode_url": qrcode_url,
            "bind_success": result.get("bind_success"),
            "bind_result": result.get("bind_result"),
            "status": result.get("status"),
        }


class CoyoteSetStrengthTool(BaseTool):
    """控制 DG-Lab A/B 通道强度。"""

    name = "coyote_set_strength"
    description = "设置郊狼 A/B 通道的强度，可选择设定绝对值或相对增减"
    parameters = [
        (
            "channel",
            ToolParamType.STRING,
            "目标通道，仅支持 A 或 B",
            True,
            ["A", "B"],
        ),
        (
            "mode",
            ToolParamType.STRING,
            "强度模式：set=设定绝对值，increase=相对增加，decrease=相对减少",
            True,
            ["set", "increase", "decrease"],
        ),
        (
            "value",
            ToolParamType.INTEGER,
            "强度数值，范围建议在 0-200 之间；对于 increase/decrease 表示增量",
            True,
            None,
        ),
    ]
    available_for_llm = True

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        if not _COYOTE_MANAGER.available:
            return {
                "name": self.name,
                "content": "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用",
                "ok": False,
            }

        server_uri = (self.get_config("connection.server_uri", "") or "").strip()
        if not server_uri:
            return {
                "name": self.name,
                "content": "未配置 DG-Lab WebSocket 服务端地址，请先通过 coyote_connect 完成初始化",
                "ok": False,
            }

        channel = str(function_args.get("channel", "")).strip()
        mode = str(function_args.get("mode", "")).strip()
        try:
            value = int(function_args.get("value"))
        except (TypeError, ValueError):
            return {
                "name": self.name,
                "content": "参数 value 无法转换为整数",
                "ok": False,
            }

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

        return {
            "name": self.name,
            "content": message,
            "ok": ok,
        }


class CoyoteAddWaveformTool(BaseTool):
    """下发 DG-Lab 波形数据。"""

    name = "coyote_add_waveform"
    description = "向郊狼设备追加波形数据，控制 A/B 通道的波形输出"
    parameters = [
        (
            "channel",
            ToolParamType.STRING,
            "目标通道，仅支持 A 或 B",
            True,
            ["A", "B"],
        ),
        (
            "pulses_json",
            ToolParamType.STRING,
            "波形数据 JSON 字符串，格式示例: "
            '[{"frequency":[10,20,30,40],"strength":[10,20,30,40]}]，'
            "frequency 为 4 个频率，strength 为 4 个强度",
            True,
            None,
        ),
    ]
    available_for_llm = True

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        if not _COYOTE_MANAGER.available:
            return {
                "name": self.name,
                "content": "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用",
                "ok": False,
            }

        server_uri = (self.get_config("connection.server_uri", "") or "").strip()
        if not server_uri:
            return {
                "name": self.name,
                "content": "未配置 DG-Lab WebSocket 服务端地址，请先通过 coyote_connect 完成初始化",
                "ok": False,
            }

        channel = str(function_args.get("channel", "")).strip()
        pulses_raw = function_args.get("pulses_json")
        if not pulses_raw:
            return {
                "name": self.name,
                "content": "缺少参数 pulses_json",
                "ok": False,
            }

        try:
            pulses_data = json.loads(pulses_raw)
        except json.JSONDecodeError as exc:
            return {
                "name": self.name,
                "content": f"解析波形 JSON 失败: {exc}",
                "ok": False,
            }

        if isinstance(pulses_data, dict):
            pulses_list = [pulses_data]
        elif isinstance(pulses_data, list):
            pulses_list = pulses_data
        else:
            return {
                "name": self.name,
                "content": "pulses_json 必须是对象或对象数组",
                "ok": False,
            }

        parsed_pulses: List[Tuple[List[int], List[int]]] = []
        for index, pulse in enumerate(pulses_list, start=1):
            if not isinstance(pulse, dict):
                return {
                    "name": self.name,
                    "content": f"第 {index} 个波形数据不是对象",
                    "ok": False,
                }
            freqs = pulse.get("frequency") or pulse.get("freq")
            strengths = pulse.get("strength") or pulse.get("amplitude")
            if not isinstance(freqs, list) or not isinstance(strengths, list):
                return {
                    "name": self.name,
                    "content": f"第 {index} 个波形数据的 frequency/strength 不是数组",
                    "ok": False,
                }
            parsed_pulses.append((freqs, strengths))

        ok, message = await _COYOTE_MANAGER.add_pulses(
            server_uri=server_uri,
            register_timeout=float(self.get_config("connection.register_timeout", 10.0)),
            channel_name=channel,
            pulses=parsed_pulses,
        )

        return {
            "name": self.name,
            "content": message,
            "ok": ok,
        }


class CoyoteClearWaveformTool(BaseTool):
    """清空指定通道的波形队列。"""

    name = "coyote_clear_waveform"
    description = "清空郊狼设备某个通道的波形队列"
    parameters = [
        (
            "channel",
            ToolParamType.STRING,
            "目标通道，仅支持 A 或 B",
            True,
            ["A", "B"],
        ),
    ]
    available_for_llm = True

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        if not _COYOTE_MANAGER.available:
            return {
                "name": self.name,
                "content": "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用",
                "ok": False,
            }

        server_uri = (self.get_config("connection.server_uri", "") or "").strip()
        if not server_uri:
            return {
                "name": self.name,
                "content": "未配置 DG-Lab WebSocket 服务端地址，请先通过 coyote_connect 完成初始化",
                "ok": False,
            }

        channel = str(function_args.get("channel", "")).strip()
        ok, message = await _COYOTE_MANAGER.clear_pulses(
            server_uri=server_uri,
            register_timeout=float(self.get_config("connection.register_timeout", 10.0)),
            channel_name=channel,
        )

        return {
            "name": self.name,
            "content": message,
            "ok": ok,
        }


class CoyotePlayPresetTool(BaseTool):
    """在指定通道播放内置预设波形。"""

    name = "coyote_play_preset"
    description = "在指定通道播放预设好的郊狼波形（steady/pulse/wave 等）"
    parameters = [
        (
            "channel",
            ToolParamType.STRING,
            "目标通道，仅支持 A 或 B",
            True,
            ["A", "B"],
        ),
        (
            "preset",
            ToolParamType.STRING,
            "预设波形名称，可选：steady（稳定）、pulse（脉冲）、wave（波浪），"
            "或放在 pulse_dir 目录下的 .pulse 文件名（不含扩展名）",
            True,
            None,
        ),
    ]
    available_for_llm = True

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        if not _COYOTE_MANAGER.available:
            return {
                "name": self.name,
                "content": "pydglab_ws 未安装或导入失败，请先安装 pydglab-ws 库或确认插件内置依赖路径可用",
                "ok": False,
            }

        server_uri = (self.get_config("connection.server_uri", "") or "").strip()
        if not server_uri:
            return {
                "name": self.name,
                "content": "未配置 DG-Lab WebSocket 服务端地址，请先通过 coyote_connect 完成初始化",
                "ok": False,
            }

        channel = str(function_args.get("channel", "")).strip()
        preset_raw = str(function_args.get("preset", "")).strip().lower()
        if preset_raw not in PRESET_PULSES:
            return {
                "name": self.name,
                "content": f"未知预设波形: {preset_raw}，当前支持: {', '.join(PRESET_PULSES.keys())}",
                "ok": False,
            }

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

        return {
            "name": self.name,
            "content": message,
            "ok": ok,
        }


@register_plugin
class DGLabCoyotePlugin(BasePlugin):
    """DG-Lab 郊狼控制插件，通过 LLM 工具调用控制强度和波形。"""

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
                default="1.0.0",
                description="配置文件版本",
            ),
            "enabled": ConfigField(
                type=bool,
                default=False,
                description="是否启用郊狼控制插件",
            ),
        },
        "connection": {
            "server_uri": ConfigField(
                type=str,
                default="ws://127.0.0.1:5678",
                description="DG-Lab WebSocket 服务端地址",
                example="ws://127.0.0.1:5678",
            ),
            "register_timeout": ConfigField(
                type=float,
                default=10.0,
                description="终端注册超时时间（秒）",
                min=1.0,
                max=60.0,
                step=1.0,
            ),
            "bind_timeout": ConfigField(
                type=float,
                default=60.0,
                description="等待 App 扫码完成绑定的时间（秒），用于连接与控制工具",
                min=5.0,
                max=600.0,
                step=5.0,
            ),
            "heartbeat_interval": ConfigField(
                type=float,
                default=20.0,
                description="内置 DG-Lab WebSocket 服务端心跳间隔（秒），数值越小保活越积极",
                min=5.0,
                max=120.0,
                step=1.0,
            ),
            "pulse_dir": ConfigField(
                type=str,
                default="pulses",
                description="存放 DungeonLab 导出 .pulse 文件的子目录（相对于插件目录）",
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
            (CoyoteConnectTool.get_tool_info(), CoyoteConnectTool),
            (CoyoteSetStrengthTool.get_tool_info(), CoyoteSetStrengthTool),
            (CoyoteAddWaveformTool.get_tool_info(), CoyoteAddWaveformTool),
            (CoyoteClearWaveformTool.get_tool_info(), CoyoteClearWaveformTool),
            (CoyotePlayPresetTool.get_tool_info(), CoyotePlayPresetTool),
        ]

    def __init__(self, plugin_dir: str):
        super().__init__(plugin_dir)
        try:
            pulse_dir = self.get_config("connection.pulse_dir", "pulses")
        except Exception:
            pulse_dir = "pulses"
        load_pulse_presets_from_dir(Path(self.plugin_dir), str(pulse_dir))
