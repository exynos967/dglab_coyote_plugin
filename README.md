# dglab_coyote_plugin

DG-Lab 郊狼控制插件，用于在 MaiBot 中通过 LLM **Action 组件**调用控制 DG-Lab Coyote（郊狼）设备的连接、强度与波形。

插件基于官方 WebSocket 协议与 `pydglab-ws` 库实现，内置简单的 WS 服务端与长连接管理，支持：

- 通过二维码完成 DG-Lab App 绑定
- 控制 A/B 通道强度（设定 / 增加 / 减少）
- 下发一次性波形
- 播放内置/自定义预设波形并循环推送
- 从 DG-Lab App 导出的 `.pulse` 波形文件加载为预设

---

## 1. 目录结构

- `plugin.py`：插件主实现，包含连接管理与所有 Action 定义
- `_manifest.json`：MaiBot 插件清单与组件（Action）暴露信息
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
config_version = "1.1.0"
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
```

---

## 4. Action 一览

插件对 LLM 暴露以下 Action（在 Action 决策系统中可见，名称与原有工具保持一致，便于迁移）：

### 4.1 `coyote_connect`

建立与 DG-Lab App 的连接并获取二维码链接的 Action。

- `action_parameters`：
  - `server_uri`（可选）：覆盖配置解析出的地址（优先级高于 `local_lan_ip + server_port + server_scheme`）
  - `register_timeout`（可选）
  - `bind_timeout`（可选）：>0 时会等待扫码结果，返回绑定状态
- 使用场景（简化自 `action_require`）：
  - 需要首次连接 / 重新连接郊狼设备、获取绑定二维码时
  - 控制强度或波形时提示未绑定 App，需要先完成扫码

### 4.2 `coyote_set_strength`

控制 A/B 通道强度的 Action。

- `action_parameters`：
  - `channel`：`"A"` 或 `"B"`
  - `mode`：`"set"`（设定绝对值）、`"increase"`（增加）、`"decrease"`（减少）
  - `value`：整型数值（0–200 推荐），`increase/decrease` 时表示增量
- 使用场景：
  - 用户明确要求“调高/调低/设为 X”时
  - 首次控制时建议从 10–30 起步，用户确认后再提高
  - 用户表示不适或要停时，将强度设为 0

### 4.3 `coyote_add_waveform`

追加一次性波形数据的 Action。

- `action_parameters`：
  - `channel`：`"A"` 或 `"B"`
  - `pulses_json`：JSON 字符串数组，每项格式：

    ```json
    [
      {
        "frequency": [10, 20, 30, 40],
        "strength":  [10, 20, 30, 40]
      }
    ]
    ```

- 每组包含 4 个频率和 4 个强度，内部会自动转换为 `add_pulses` 所需的结构。

### 4.4 `coyote_clear_waveform`

清空指定通道的波形队列并停止该通道循环任务的 Action。

- `action_parameters`：
  - `channel`：`"A"` 或 `"B"`

用于停止持续输出或在切换预设前清空队列。

### 4.5 `coyote_play_preset`

在指定通道上播放预设波形（**循环模式**）的 Action，包括内置预设与 `.pulse` 文件预设。

- `action_parameters`：
  - `channel`：`"A"` 或 `"B"`
  - `preset`：预设名称：
    - 内置：`steady`、`pulse`、`wave`
    - `.pulse` 文件：文件名（不含扩展名），例如 `128-星儿`
- 行为：
  - 先立即向该通道下发一次完整波形
  - 然后启动后台任务，周期性重复调用 `add_pulses` 保持持续输出
  - 若同一通道已有循环任务，会先取消旧任务再启动新任务
  - 要停止循环，可调用 `coyote_clear_waveform` 或将强度调为 0

---

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

4. 通过 `coyote_play_preset` 播放对应预设：

   - `preset = "128-星儿"`
   - 或 `preset = "my-custom"`

> 说明：当前解析逻辑会从 `.pulse` 文件中提取强度曲线，并用固定频率构造近似波形，同时对长度做了安全截断，以满足 DG-Lab 协议限制。

---

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

4. **控制强度与波形**
   - 通过 `coyote_set_strength` 调整 A/B 通道强度
   - 使用 `coyote_play_preset` 播放内置或 `.pulse` 预设（循环模式）
   - 使用 `coyote_clear_waveform` 停止某个通道的波形输出

---

## 7. 日志说明

插件使用 `dglab_coyote_plugin` 日志频道输出关键事件，例如：

- `[Coyote] 启动内置 DG-Lab WebSocket 服务端: ws://...`
- `[Coyote] App 绑定成功（ensure_bind 完成）`
- `[Coyote] App 断开连接（CLIENT_DISCONNECTED）`
- `[Coyote] 设置强度成功: channel=A, mode=INCREASE, value=20`
- `[Coyote] 下发波形成功: channel=A, pulses_count=1`
- `[Coyote] 启动波形循环: channel=A, pulses_count=N`
- `[Coyote] 清空波形队列成功: channel=A`

通过这些日志可以快速判断当前连接状态、绑定情况以及控制是否生效，便于在 MaiBot 中排查问题。
