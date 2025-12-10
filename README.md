# dglab_coyote_plugin

DG-Lab 郊狼控制插件，用于在 MaiBot 中通过 LLM 工具调用控制 DG-Lab Coyote（郊狼）设备的连接、强度与波形。

插件基于官方 WebSocket 协议与 `pydglab-ws` 库实现，内置简单的 WS 服务端与长连接管理，支持：

- 通过二维码完成 DG-Lab App 绑定
- 控制 A/B 通道强度（设定 / 增加 / 减少）
- 下发一次性波形
- 播放内置/自定义预设波形并循环推送
- 从 DG-Lab App 导出的 `.pulse` 波形文件加载为预设

---

## 1. 目录结构

- `plugin.py`：插件主实现，包含连接管理与所有工具定义
- `_manifest.json`：MaiBot 插件清单与工具暴露信息
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
config_version = "1.0.0"
enabled = true          # 是否启用插件
```

### 3.2 连接参数

```toml
[connection]
# DG-Lab WebSocket 服务端地址，一般使用本机 WS 服务
server_uri = "ws://127.0.0.1:5678"

# 终端注册超时时间（秒），通常保持默认即可
register_timeout = 10.0

# 等待 App 扫码完成绑定的时间（秒）
bind_timeout = 60.0

# 内置 DG-Lab WebSocket 服务端心跳间隔（秒），数值越小，保活越积极
heartbeat_interval = 20.0

# 存放 DungeonLab 导出 .pulse 文件的子目录（相对于插件目录）
pulse_dir = "pulses"
```

### 3.3 控制参数

```toml
[control]
# 强度上限（0-200），set 模式会自动裁剪到此上限
max_intensity = 200
```

---

## 4. 工具一览

插件对 LLM 暴露以下工具（工具名称在日志中会以 `工具xxx` 的形式出现）：

### 4.1 `coyote_connect`

建立与 DG-Lab App 的连接并获取二维码链接。

- 参数：
  - `server_uri`（可选）：覆盖配置里的 `connection.server_uri`
  - `register_timeout`（可选）
  - `bind_timeout`（可选）：>0 时会等待扫码结果，返回绑定状态
- 用途：
  - 插件启动后 **至少调用一次**，扫码绑定 App
  - 返回字段中包含：
    - `qrcode_url`：用于 App 扫码的链接
    - `bind_result`：`SUCCESS` / `TIMEOUT` / 错误信息

### 4.2 `coyote_set_strength`

控制 A/B 通道强度。

- 参数：
  - `channel`：`"A"` 或 `"B"`
  - `mode`：`"set"`（设定绝对值）、`"increase"`（增加）、`"decrease"`（减少）
  - `value`：整型数值（0–200 推荐），`increase/decrease` 时表示增量
- 行为：
  - 会自动调用内部的 `ensure_bind`，在需要时等待 App 完成绑定
  - `set` 模式会按照 `control.max_intensity` 限制最大值

### 4.3 `coyote_add_waveform`

追加一次性波形数据。

- 参数：
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

清空指定通道的波形队列，并停止该通道的循环任务。

- 参数：
  - `channel`：`"A"` 或 `"B"`

用于停止持续输出或在切换预设前清空队列。

### 4.5 `coyote_play_preset`

在指定通道上播放预设波形（**循环模式**），包括内置预设与 `.pulse` 文件预设。

- 参数：
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
   mkdir -p dglab_coyote_plugin/pulses
   ```

2. 将 App 导出的 `.pulse` 文件放入该目录，例如：

   ```text
   dglab_coyote_plugin/
     pulses/
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
     - `[connection].server_uri` 设置为本机 DG-Lab WS 地址

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

