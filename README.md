# dglab_coyote_plugin

DG-Lab 郊狼控制插件，用于在 MaiBot 中通过用户 **Command 指令**控制 DG-Lab Coyote（郊狼）设备的连接与通道输出。

插件基于官方 WebSocket 协议与 `pydglab-ws` 库实现，内置简单的 WS 服务端与长连接管理，支持：

- 通过二维码完成 DG-Lab App 绑定
- 控制 A/B 通道强度（设定 / 增加 / 减少）
- 下发一次性波形
- 播放内置/自定义预设波形并循环推送
- 从 DG-Lab App 导出的 `.pulse` 波形文件加载为预设
- 支持连接/断连设备、开启/关闭通道输出的显式指令

---

## 1. 目录结构

- `plugin.py`：插件主实现，包含连接管理与所有 Command 定义
- `_manifest.json`：MaiBot 插件清单与组件（Command）暴露信息
- `config.toml`：插件配置文件（运行后由宿主在插件目录生成）
- `pulses/`：可选目录，存放从 DG-Lab App 导出的 `.pulse` 波形文件

---

## 2. 依赖与兼容性

插件依赖：

- Python 3.10+
- `pydglab-ws>=1.1.0`（在 `_manifest.json` 中通过 `python_dependencies` 声明）
- 与 `pydglab-ws` 兼容的 `websockets` 版本（推荐 `websockets==12.0`）

一般情况下，由 MaiBot 的依赖安装器根据 `_manifest.json` 自动安装 `pydglab-ws`；如果需要手动安装，可执行：

```bash
pip install "pydglab-ws>=1.1.0" "websockets==12.0"
```

插件内部也包含了一个 `pydglab_ws-1.1.0` 目录，在外部环境未安装时会自动尝试从该目录导入。

---

## 3. 配置说明（config.toml）

插件配置通过 `config.toml` 管理，主要包含三个 section：`[plugin]`、`[connection]`、`[control]`。

### 3.1 基本配置

```toml
[plugin]
config_version = "1.3.0"
enabled = true          # 是否启用插件
```

### 3.2 连接参数

```toml
[connection]
# 运行插件机器的局域网 IP（默认 127.0.0.1）
# 重要：服务器部署时必须改成手机可访问到的局域网 IP，例如 192.168.1.23
local_lan_ip = "127.0.0.1"

# DG-Lab WebSocket 端口
server_port = 5678

# 协议（ws / wss）
server_scheme = "ws"

# 可选：完整地址，填写后会覆盖上面三项组合
server_uri = ""

# 终端注册超时时间（秒），通常保持默认即可
register_timeout = 10.0

# 等待 App 扫码完成绑定的时间（秒）
bind_timeout = 60.0

# 内置 DG-Lab WebSocket 服务端心跳间隔（秒），数值越小，保活越积极
heartbeat_interval = 20.0

# 存放 DungeonLab 导出 .pulse 文件的子目录（相对于插件目录）
pulse_dir = "pulse_dir"
```

### 3.3 控制参数

```toml
[control]
# 强度上限（0-200），set 模式会自动裁剪到此上限
max_intensity = 200

# /coyote_channel_on 未传 preset 时使用的默认预设
command_default_preset = "steady"

# /coyote_channel_on 未传 strength 时使用的默认强度
command_default_strength = 30
```

---

## 4. Command 指令一览

以下是新增的用户指令（命令组件）：

- `/coyote_connect [bind_timeout]` 或 `/郊狼连接 [bind_timeout]`
  - 连接设备并返回二维码，可选覆盖等待绑定超时（秒）
- `/coyote_disconnect` 或 `/郊狼断连`
  - 断开设备连接，并自动关闭插件内置 WS 服务端
- `/coyote_channel_on <A|B> [preset] [strength]` 或 `/郊狼开启通道 <A|B> [preset] [strength]`
  - 开启指定通道输出：启动预设循环波形并设置强度
- `/coyote_channel_off <A|B>` 或 `/郊狼关闭通道 <A|B>`
  - 关闭指定通道输出：强度归零并清空通道波形

## 5. 使用 `.pulse` 波形文件

插件支持从 DG-Lab App 导出的 `.pulse` 文件（以 `Dungeonlab+pulse:` 开头的文本），在启动时自动解析为预设波形：

1. 在插件目录下创建子目录（若不存在）：

   ```bash
   mkdir -p dglab_coyote_plugin/pulse_dir
   ```

2. 将 App 导出的 `.pulse` 文件放入该目录，例如：

   ```text
   dglab_coyote_plugin/
     pulse_dir/
       128-星儿.pulse
       my-custom.pulse
   ```

3. 重新启动 MaiBot / 重新加载插件。

4. 通过 `coyote_channel_on` 指令播放对应预设：

   - `/coyote_channel_on A 128-星儿`
   - `/coyote_channel_on B my-custom`

> 说明：当前解析逻辑会从 `.pulse` 文件中提取强度曲线，并用固定频率构造近似波形，同时对长度做了安全截断，以满足 DG-Lab 协议限制。

## 6. 典型使用流程

1. **安装插件与依赖**
   - 将 `dglab_coyote_plugin` 放入 MaiBot 的插件目录
   - 确保环境中已安装 `pydglab-ws`（或使用插件自带的库）

2. **配置并启用插件**
   - 在 `config.toml` 中设置：
     - `[plugin].enabled = true`
     - 本地直连可保持 `[connection].local_lan_ip = 127.0.0.1`
     - 如果插件运行在服务器/远端主机，必须把 `[connection].local_lan_ip` 改为该主机的局域网 IP（与手机在同一网段）
     - 可选：按需调整 `[connection].server_port` 与 `[connection].server_scheme`

3. **绑定 DG-Lab App**
   - 调用 `coyote_connect`
   - 使用 DG-Lab App 扫码日志中的二维码链接
   - 绑定成功后日志中会出现：
     - `[Coyote] App 绑定成功（ensure_bind 完成）`

4. **通过指令启停通道 / 连接管理**
   - 连接：`/coyote_connect`
   - 开启 A 通道（默认预设/强度）：`/coyote_channel_on A`
   - 开启 B 通道（指定预设和强度）：`/coyote_channel_on B pulse 40`
   - 关闭通道：`/coyote_channel_off A`
   - 断连并关闭 WS 服务端：`/coyote_disconnect`

## 7. 日志说明

插件使用 `dglab_coyote_plugin` 日志频道输出关键事件，例如：

- `[Coyote] 启动内置 DG-Lab WebSocket 服务端: ws://...`
- `[Coyote] App 绑定成功（ensure_bind 完成）`
- `[Coyote] App 断开连接（CLIENT_DISCONNECTED）`
- `[Coyote] 设置强度成功: channel=A, mode=INCREASE, value=20`
- `[Coyote] 下发波形成功: channel=A, pulses_count=1`
- `[Coyote] 启动波形循环: channel=A, pulses_count=N`
- `[Coyote] 清空波形队列成功: channel=A`
- `[Coyote] 断连时通道清理存在失败: ...`（若断连前清理出现异常）

通过这些日志可以快速判断当前连接状态、绑定情况以及控制是否生效，便于在 MaiBot 中排查问题。
