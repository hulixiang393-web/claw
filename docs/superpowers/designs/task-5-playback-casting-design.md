# Task 5：播放界面投屏接入与双向控制 - 详细设计文档

**日期**：2026-10-03  
**任务**：播放界面投屏按钮、状态展示、双向控制、本地 VLC 关闭顺序、状态同步与测试策略  
**相关模块**：`gui/pages/reader/video_view.py`、`framework/media_proxy.py`、`framework/dlna.py`、`gui/components/cast_dialog.py`

---

## 1. 播放控制条的投屏按钮

### 1.1 位置与布局

- **位置**：`video_view.py:600` 附近，播放控制条内
- **相对位置**：换源（source）按钮附近，建议在换源按钮右侧或左侧保留逻辑关联
- **布局约束**：不与现有播放/暂停/进度条冲突；保持控制条视觉协调

### 1.2 图标与文案

| 状态 | 图标 | 提示文本 | 备注 |
|------|------|--------|------|
| 未投屏（idle） | `📺` | 「投屏」| 标准状态，可点击 |
| 投屏中（casting） | `📺` | 「停止投屏」 | 图标不变，文案变更；鼠标悬停显示设备名 |
| 准备中（preparing） | `📺` + 微旋转指示 | 「正在投屏...」 | 禁用点击，显示加载状态 |
| 错误状态（error） | `📺` + 红感叹 | 「投屏失败」 | 禁用点击，悬停显示错误信息 |

### 1.3 禁用条件

按钮禁用的场景（灰显 + 不响应点击）：

1. **无源**：当前视频源为空或 `_current_source` 为 None
2. **无当前集**：`_current_idx == -1` 或集列表为空
3. **正在准备中**：投屏启动流程进行中（`cast_state == "preparing_stream"` 或 `"connecting"`）
4. **获取流失败**：当前集无法获取真实流地址
5. **外部 VLC 模式 + 本地播放中**：允许启用；但提示「启动投屏会停止本地 VLC」

### 1.4 状态变化与转移

```
idle ──点击──> scanning ──成功──> preparing_stream ──SetURI──> connecting
                                       │
                                   ───失败──> error
                                       │
                                       └──PLAYING──> playing (cast_active=True)

playing ──遥控器暂停──> paused
paused  ──遥控器播放──> playing

playing/paused ──点击停止投屏──> idle
error ──点击重试──> preparing_stream 或 scanning
```

### 1.5 按钮与外部 VLC 模式的交互

- **外部 VLC 模式下**：按钮仍可用；启动投屏会显示「确认」对话框：「启动投屏会停止本地 VLC 播放，是否继续？」
- **投屏模式启动成功后**（`cast_active == True`）：按钮文案变为「停止投屏」，点击触发 `_on_cast_stop()`

---

## 2. 状态展示区域

### 2.1 区域位置与构成

在播放控制条下方或控制条内预留展示区域，分为两个层级：

**第一层（始终可见）**：
- 投屏设备名（当投屏激活时）
- 当前播放集的标题或索引

**第二层（投屏激活时展开）**：
- 投屏状态消息
- 进度时间显示
- 错误提示与操作按钮

### 2.2 状态消息文案

| 状态 | 消息文案 | 额外显示 |
|------|--------|--------|
| scanning | 「正在搜索投屏设备...」 | 无 |
| preparing_stream | 「正在准备播放流...」 | 无 |
| connecting | 「正在连接电视 [设备名]」 | 设备名 |
| playing | 「投屏中 - [设备名]」 | 进度条、当前时间、总时长 |
| paused | 「已暂停 - [设备名]」 | 进度条、暂停时刻、总时长 |
| stopped | 「已停止」 | 无 |
| error | 「投屏失败」 | 错误代码与简短描述 |

### 2.3 进度与时间显示

**投屏激活时** (`cast_active == True`)：

1. **进度条**：
   - 显示当前播放进度的 slider（只读或可拖拽）
   - 拖拽时立即发送 `DlnaClient.seek(seconds)` 请求
   - 若电视遥控器调整，轮询刷新进度条位置

2. **时间标签**：
   - 格式：`HH:MM:SS / HH:MM:SS`（当前时间 / 总时长）
   - 每 1 秒更新一次（从轮询数据）

3. **当前集标题**：
   - 显示集列表中当前正在播放集的标题
   - 电视遥控器切集时，若支持 Track 字段，更新显示集序号

### 2.4 投屏状态消息区

```
┌─────────────────────────────────┐
│ 📺 投屏中 - Samsung TV (192.168.1.100)
│ 第 3 集：剧情高潮
│ 00:12:34 / 00:45:00
│
│ [进度条 ████░░░░░░░░░░░░░░░░░]
│
│ [停止投屏] [设置]
└─────────────────────────────────┘
```

### 2.5 错误提示与恢复操作

错误消息格式：

```
投屏失败
错误码：AVTransport.InvalidURI (701)
设备可能不支持该媒体格式或当前离线

[重试] [更换设备] [关闭投屏]
```

常见错误处理：

| 错误 | 提示文案 | 恢复操作 |
|-----|--------|--------|
| 设备离线 | 「设备已离线，请检查网络」 | [重试] / [关闭投屏] |
| 不支持格式 | 「电视不支持该媒体格式」 | [更换源] / [关闭投屏] |
| SOAP 错误 | 「与电视通讯失败」 + 错误码 | [重试] / [关闭投屏] |
| 防火墙阻止 | 「Windows 防火墙阻止了投屏端口，请在防火墙允许 Claw」 | [关闭投屏] |
| 网络错误 | 「无法找到电视，请确认处于同一局域网」 | [扫描] / [取消] |

---

## 3. App 控制路由

### 3.1 控制判断条件

在所有播放控制操作的入口处添加分支判断：

```python
def _on_play_clicked(self):
    if self.cast_active:
        # 路由到 DLNA
        self._dlna_client.play()
    else:
        # 保留现有本地 VLC / 外部播放器逻辑
        self._player.play()

def _on_pause_clicked(self):
    if self.cast_active:
        self._dlna_client.pause()
    else:
        self._player.pause()

def _on_seek(self, seconds: int):
    if self.cast_active:
        self._dlna_client.seek(seconds)
    else:
        self._player.seek(seconds)
```

### 3.2 逐一控制的委托逻辑

#### 3.2.1 播放（Play）

**条件**：`cast_active == True` 且状态为 `paused` 或 `stopped`

**流程**：
1. UI 禁用播放按钮，显示加载指示
2. 调用 `DlnaClient.play()`
3. 等待轮询更新状态到 `PLAYING`（超时 3 秒）
4. 成功：恢复播放按钮状态、更新进度条
5. 失败：显示错误，按钮恢复可用

**异常处理**：
- SOAP 错误 → 捕获并显示错误提示
- 网络超时 → 记日志，标记 `error` 状态

#### 3.2.2 暂停（Pause）

**条件**：`cast_active == True` 且状态为 `playing`

**流程**：
1. 调用 `DlnaClient.pause()`
2. 轮询等待状态变为 `PAUSED_PLAYBACK`（超时 3 秒）
3. UI 更新暂停按钮状态、冻结进度条

#### 3.2.3 进度条（Seek）

**条件**：`cast_active == True`

**流程**：
1. 用户拖拽进度条滑块
2. 记录目标秒数 `target_seconds`
3. 调用 `DlnaClient.seek(target_seconds)`
4. 不等待确认，直接更新本地 UI 进度条显示（乐观更新）
5. 轮询会自动校准实际进度

**超前防护**：
- 若进度条被拖到尾部，自动停止投屏（可选）
- 若 Seek 失败超过 2 次，标记设备不可靠，显示警告

#### 3.2.4 上一集（Previous）

**条件**：`cast_active == True`

**流程**：
1. 计算上一集索引：`prev_idx = self._current_idx - 1`
2. 若 `prev_idx < 0`，禁用或提示「已是第一集」
3. 从集列表获取上一集信息（标题、原始 URL）
4. 调用 `_get_stream_url(prev_idx)` 获取真实流地址（若缓存命中则复用）
5. 调用 `DlnaClient.set_av_transport_uri()` 设置新集 URI
6. 调用 `DlnaClient.play()`
7. 轮询更新 `_current_idx` 和 UI 集标题
8. 失败：保留当前集播放，提示「切集失败」

**缓存策略**：
- 若上一集已在 `_stream_cache` 中，直接取用
- 否则触发后台 `_FetchStreamTask` 获取

#### 3.2.5 下一集（Next）

**条件**：`cast_active == True`

**流程**：
1. 计算下一集索引：`next_idx = self._current_idx + 1`
2. 若 `next_idx >= len(series_list)`，禁用或提示「已是最后一集」
3. 获取下一集流地址（同上一集逻辑）
4. **尝试设置 NextAVTransportURI**：
   - 若设备支持，调用 `DlnaClient.set_next_av_transport_uri()`
   - 设备执行完当前集后自动播放下一集（无缝播放）
5. **若 NextAVTransportURI 不支持**，等待当前集结束信号：
   - 轮询检测 `CurrentTransportState == STOPPED`
   - 触发 `_on_next_episode()` 重新设置 URI 并播放
6. UI 更新「下一集」预览（可选）

**双向兼容**：
- 电视遥控器的下一集仅在设备支持 `NextAVTransportURI` 时生效
- 设备不支持时，App 下一集始终可靠；遥控器下一集显示不可用提示

### 3.3 停止投屏（Stop Casting）

**触发**：
- 用户点击「停止投屏」按钮
- 切换源、切换作品、关闭播放页、应用退出

**流程**：
1. 调用 `DlnaClient.stop()`
2. 等待状态变为 `STOPPED` 或超时 2 秒
3. 清理 `CastSession`（设备对象、token、轮询 worker）
4. 调用 `MediaProxy.release_lan_tokens()` 清理投屏端点 token
5. 调用 `MediaProxy.stop_lan_endpoint()` 关闭局域网端点
6. 重置 UI 状态：
   - 投屏按钮恢复「投屏」文案
   - 状态消息区隐藏
   - 进度条恢复本地模式（若有本地播放）
7. 设置 `cast_active = False`

---

## 4. 电视遥控器状态同步

### 4.1 轮询机制

**轮询 Worker 线程**（基于 `QThreadPool` 或现有任务模式）：

```python
class CastPollWorker(QRunnable):
    def __init__(self, dlna_client, interval=1.0):
        self.dlna_client = dlna_client
        self.interval = interval
        self._running = True
    
    def run(self):
        while self._running:
            try:
                # 获取传输状态
                transport_info = self.dlna_client.get_transport_info()
                # 获取位置信息（进度、时长）
                position_info = self.dlna_client.get_position_info()
                
                # 发射信号给主线程
                self.state_changed.emit(transport_info, position_info)
                
                time.sleep(self.interval)
            except Exception as e:
                # 连续失败计数
                self.error_count += 1
                if self.error_count >= 3:
                    self.poll_failed.emit(str(e))
                    self._running = False
                else:
                    time.sleep(self.interval)
```

**轮询周期**：1 秒  
**连续失败阈值**：3 次失败后停止轮询，标记 `error` 状态  
**信号定义**：
- `state_changed(transport_info, position_info)` → 主线程槽更新 UI
- `poll_failed(error_msg)` → 关闭投屏、显示错误

### 4.2 进度条反映

**轮询更新进度条**：

```python
def _on_cast_state_changed(self, transport_info, position_info):
    """轮询信号槽：更新进度条、时间标签、播放按钮"""
    
    # 提取位置信息
    current_time = position_info.get('RelTime')  # 秒数或 None
    total_time = position_info.get('TrackDuration')  # 秒数或 None
    
    if current_time is not None:
        # 更新 slider 值（不触发 valueChanged 信号）
        self.progress_slider.blockSignals(True)
        self.progress_slider.setValue(current_time)
        self.progress_slider.blockSignals(False)
    
    if current_time and total_time:
        # 更新时间标签
        self.time_label.setText(
            f"{self._format_time(current_time)} / {self._format_time(total_time)}"
        )
    
    # 更新传输状态
    state = transport_info.get('CurrentTransportState')
    self._update_transport_state(state)
```

**避免用户拖拽冲突**：
- 用户正在拖拽进度条时，暂停轮询更新 slider 值
- 释放滑块后，恢复轮询同步

### 4.3 播放/暂停按钮状态同步

**状态映射**：

```python
def _update_transport_state(self, state: str):
    """根据电视状态更新按钮"""
    
    state_map = {
        'PLAYING': self._set_playing_state,
        'PAUSED_PLAYBACK': self._set_paused_state,
        'STOPPED': self._set_stopped_state,
        'TRANSITIONING': self._set_transitioning_state,
        'NO_MEDIA_PRESENT': self._set_no_media_state,
    }
    
    handler = state_map.get(state)
    if handler:
        handler()
    
    self.cast_state = state  # 记录当前状态
```

**各状态对应的按钮/UI 更新**：

| 状态 | 播放按钮 | 暂停按钮 | 进度条 | 消息 |
|-----|---------|---------|-------|------|
| PLAYING | 禁用 | 启用 | 更新位置 | 「投屏中」 |
| PAUSED_PLAYBACK | 启用 | 禁用 | 冻结位置 | 「已暂停」 |
| STOPPED | 启用 | 禁用 | 清零 | 「已停止」 |
| TRANSITIONING | 禁用 | 禁用 | 冻结位置 | 「正在切集...」 |
| NO_MEDIA_PRESENT | 启用 | 禁用 | 无效 | 「电视无媒体」 |

### 4.4 当前集同步

**从轮询数据获取**：

```python
def _update_current_track(self, position_info: dict):
    """根据电视返回的 Track 号更新 UI 集列表选中状态"""
    
    track_num = position_info.get('Track')
    if track_num is not None and track_num >= 0:
        # 映射 Track 号到集索引（可能需要偏移）
        idx = track_num - 1  # 假设 Track 从 1 开始
        
        if idx != self._current_idx:
            self._current_idx = idx
            self.series_list.setCurrentRow(idx)
            # 更新集标题显示
            self._update_episode_title()
```

**异常处理**：
- Track 为 0 或负数时，保留当前 `_current_idx`
- 电视不支持 Track 字段时，仅依赖 App 的下一集/上一集 SetAVTransportURI 更新

### 4.5 错误恢复与断开检测

**轮询失败处理**：

1. **第 1、2 次失败**：记日志，继续轮询
2. **第 3 次失败**：
   - 停止轮询
   - 标记 `cast_state = 'error'`
   - 显示错误提示：「与电视失去连接」
   - 按钮显示「重试」和「关闭投屏」

**重试逻辑**：
- 用户点击「重试」时，重启轮询 worker
- 若轮询恢复，自动切换到投屏状态
- 若再次失败，提示「连接不稳定，请检查网络」

---

## 5. 本地 VLC 关闭顺序与时序

### 5.1 投屏启动流程中的 VLC 关闭时机

**关键原则**：先确保电视成功播放，再停止本地 VLC，避免黑屏。

**完整流程时序**：

```
时刻 T0：用户点击「投屏」按钮
  └─ 本地 VLC：继续播放（未改变）

T1：开始 SSDP 扫描
  └─ 本地 VLC：继续播放

T2：用户选择设备 → 获取当前集真实流地址
  └─ 本地 VLC：继续播放

T3：启动投屏专用 LAN 代理端点（listening on 192.168.1.x:port）
  └─ 本地 VLC：继续播放
  └─ 本地代理端点：仍在 127.0.0.1，不受影响

T4：调用 SetAVTransportURI + metadata
  └─ 本地 VLC：继续播放
  └─ 电视：收到 URI，准备播放

T5：调用 Play
  └─ 本地 VLC：继续播放
  └─ 电视：开始 TRANSITIONING → PLAYING

T6：启动轮询 worker，检查 CurrentTransportState
  └─ 本地 VLC：继续播放
  └─ 轮询：每 1 秒检查电视状态

T7（最多 8 秒后）：轮询得到 state == PLAYING
  ✓ 投屏确认成功！
  └─ 调用 stop_local_vlc()  ← **此刻才停止**

T8：PLAYING 确认后
  └─ 本地 VLC：已停止
  └─ 电视：播放中
  └─ cast_active = True
```

### 5.2 何时停止本地 VLC

**停止条件**（满足其一）：

1. **投屏确认成功**：轮询首次收到 `CurrentTransportState == PLAYING`
2. **用户主动关闭投屏**：点击「停止投屏」按钮
3. **应用退出**：清理所有投屏资源

**不停止本地 VLC 的场景**（保留备份）：

- 投屏启动失败（SetAVTransportURI 或 Play 返回 SOAP 错误）
- 轮询等待 PLAYING 超时（8 秒内未收到）
- 电视遥控器突然停止播放（电视用户主动停止）→ 继续轮询，不自动重启本地 VLC

### 5.3 Token 清理的分阶段

**投屏端点专用 token** 与 **本地 VLC 的 token** 需要分离：

#### 阶段 1：投屏启动成功，电视确认 PLAYING

```python
# 此刻已清理
MediaProxy.release_lan_tokens()  # 清理投屏端 token
# 但不清理 local_endpoint token（本地 VLC 继续使用）
```

#### 阶段 2：停止投屏或应用退出

```python
# 清理投屏会话
DlnaClient.stop()
CastSession.cleanup()

# 清理投屏专用资源
MediaProxy.stop_lan_endpoint()  # 关闭 LAN 监听端口
MediaProxy.release_lan_tokens()  # 清理投屏 token

# 本地 VLC token 保留（若本地 VLC 仍在运行）
# 或在 VLC 停止后主动清理
```

#### 阶段 3：重新启动本地播放

```python
# 用户点击本地播放（投屏关闭后）
# VLC 使用原有 127.0.0.1 端点的 token，无需重新生成
player.play()
```

### 5.4 电视确认 PLAYING 的超时与重试

**首次确认超时**：

```python
def _wait_for_playing(self, max_wait=8.0):
    """轮询等待电视进入 PLAYING 状态"""
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        transport_info = self.dlna_client.get_transport_info()
        state = transport_info.get('CurrentTransportState')
        
        if state == 'PLAYING':
            return True  # ✓ 成功，停止本地 VLC
        
        if state == 'NO_MEDIA_PRESENT':
            # 电视无媒体，可能格式不支持
            raise Exception("电视不支持该媒体格式或连接断开")
        
        time.sleep(1.0)
    
    # 超时
    raise TimeoutError("电视未在 8 秒内开始播放")
```

**超时后的行为**：

1. 显示错误提示：「电视未在规定时间内开始播放」
2. **不自动重启本地 VLC**（避免用户困惑）
3. 用户选择：
   - 「重试」：重新执行 Play 流程
   - 「关闭投屏」：停止 LAN 端点，恢复本地播放

### 5.5 VLC 关闭函数签名

```python
def stop_local_vlc(self):
    """停止本地 VLC 播放"""
    if self._player and self._player.is_playing():
        self._player.stop()
        self._vlc_instance = None  # 释放 VLC 实例
    
    # 日志记录
    logger.info(f"Local VLC stopped after cast confirmed on {self.device_name}")
```

**调用位置**：
- `_on_cast_playing()` - 轮询首次获得 PLAYING 状态的槽函数
- `_cleanup_cast_session()` - 投屏会话清理时

---

## 6. 测试策略

### 6.1 单元测试

#### 6.1.1 `tests/test_dlna.py` - DLNA 协议与解析

**测试用例**：

1. **SSDP 设备响应解析**
   - 输入：标准 SSDP M-SEARCH 响应报文
   - 预期：提取 UDN、friendly name、LOCATION、control URL
   - 验证：无效 XML、缺失字段时正确跳过

2. **设备去重**
   - 输入：两个 SSDP 响应（同一 UDN 的不同 IP）
   - 预期：仅保留 UDN 唯一的设备
   - 验证：列表长度 == 1

3. **SOAP Action 请求体构造**
   - 测试：`play()`、`pause()`、`seek()`、`stop()` 方法生成的 XML
   - 验证：SOAPACTION header、方法名、参数格式正确

4. **SOAP 错误码解析**
   - 输入：SOAP Fault 响应（如 `701 InvalidURI`）
   - 预期：异常类型为 `DlnaError`，错误码、描述正确

5. **时间格式解析**
   - 测试：`RelTime` 与 `TrackDuration` 字段
   - 样例：`"00:12:34"` → `754` 秒，`"HH:MM:SS"` 无效 → `None`
   - 验证：所有合法格式转换正确，非法返回 None 不抛异常

6. **DIDL-Lite metadata 生成**
   - 测试：为 mp4、HLS 各生成 metadata
   - 验证：
     - `dc:title` 与输入一致
     - `upnp:class` == `object.item.videoItem`
     - `res protocolInfo` 正确（`application/vnd.apple.mpegurl` for HLS）
     - `res` URI 采用指定的 `public_base`

#### 6.1.2 `tests/test_media_proxy_lan.py` - 局域网代理端点

**测试用例**：

1. **本地 URL 默认行为不变**
   - 调用 `build_url()` 不传 `public_base`
   - 预期：返回 `http://127.0.0.1:<port>/s/<token>`

2. **public_base 生成局域网 URL**
   - 调用 `build_url(..., public_base="http://192.168.1.10:9000")`
   - 预期：返回 `http://192.168.1.10:9000/s/<token>`

3. **m3u8 内部资源使用局域网 base**
   - 请求 `.m3u8` 并指定 `public_base`
   - 预期：响应内所有分片 URL、密钥 URL 均为 `http://192.168.1.10:9000/...`
   - 验证：派生 token 登记到投屏 token 集合

4. **本地 URL token 与投屏 URL token 隔离**
   - 同一资源，分别用本地和投屏 URL 请求
   - 预期：生成两个不同的 token，各自记在对应集合中

5. **局域网端点停止不清理本地端点**
   - 调用 `stop_lan_endpoint()`
   - 预期：
     - LAN 监听端口关闭
     - 投屏 token 集合清空
     - 本地端点仍监听 127.0.0.1，token 保留

6. **token、请求头和广告过滤仍正确**
   - 投屏 URL 测试：广告过滤、防盗链 Referer 保存、Range 206
   - 预期：功能与本地 URL 一致

#### 6.1.3 `tests/test_video_cast.py` - 播放界面投屏逻辑

**测试用例**：

1. **投屏按钮初始状态**
   - 无源或无当前集时，投屏按钮禁用
   - 验证：`cast_btn.isEnabled() == False`

2. **点击投屏使用当前 _current_idx**
   - 设置 `_current_idx = 2`
   - 点击「投屏」→ 选择设备 → 开始投屏
   - 预期：上传的集标题与 `series_list[2]` 一致

3. **当前流缓存命中**
   - 第一次投屏获取流，记入 `_stream_cache`
   - 第二次投屏同一集
   - 预期：不重复调用 `_FetchStreamTask`，直接使用缓存 URL

4. **电视确认 PLAYING 后才停止本地 VLC**
   - mock `DlnaClient.get_transport_info()` 返回 `TRANSITIONING` → `PLAYING`
   - 预期：
     - 收到 `TRANSITIONING` 时，本地 VLC 未停止
     - 收到 `PLAYING` 时，`stop_local_vlc()` 被调用

5. **DLNA 失败时本地 VLC 不关闭**
   - mock `DlnaClient.set_av_transport_uri()` 抛异常
   - 预期：
     - `stop_local_vlc()` 未被调用
     - UI 显示错误提示
     - 本地 VLC 继续运行

6. **播放/暂停/Seek 路由到 DLNA**
   - 投屏激活状态下，依次点击播放、暂停、拖动进度条
   - 验证：
     - `_dlna_client.play()` 被调用
     - `_dlna_client.pause()` 被调用
     - `_dlna_client.seek(seconds)` 被调用

7. **上一集/下一集切集流程**
   - 投屏中，点击「下一集」
   - 预期：
     - 调用 `_get_stream_url(next_idx)`
     - 调用 `_dlna_client.set_av_transport_uri()`
     - 调用 `_dlna_client.play()`
     - `_current_idx` 更新

8. **轮询状态更新 UI 信号**
   - mock 轮询 worker 发出 `state_changed` 信号
   - 预期：
     - 进度条更新
     - 时间标签更新
     - 播放按钮状态同步

9. **停止投屏清理资源**
   - 投屏中，点击「停止投屏」
   - 验证调用顺序：
     1. `_dlna_client.stop()`
     2. `_cast_poll_worker.stop()`
     3. `MediaProxy.release_lan_tokens()`
     4. `MediaProxy.stop_lan_endpoint()`
   - 验证 `cast_active == False`、按钮恢复「投屏」

### 6.2 集成测试

#### 6.2.1 CastSession Mock + UI 操作集成

**测试场景**：

1. **完整投屏生命周期模拟**
   - mock `DlnaDiscovery.discover()` 返回模拟设备列表
   - mock `DlnaClient` 所有 SOAP 操作
   - 操作流程：
     1. 点击「投屏」
     2. 对话框出现，选择模拟设备
     3. 投屏启动，状态消息变更
     4. 轮询数据更新，进度条移动
     5. 点击「停止投屏」
   - 验证：UI 状态转移、信号发射、资源清理

2. **用户交互冲突处理**
   - 投屏启动中，再次点击「投屏」
   - 预期：第二次点击被忽略或显示提示「投屏进行中」

3. **轮询失败降级**
   - mock 轮询 worker 连续 3 次失败
   - 预期：
     - 轮询停止
     - UI 显示「投屏失败」状态
     - 用户可点击「重试」或「关闭投屏」

#### 6.2.2 源/作品切换时的投屏清理

**测试场景**：

1. **投屏中切换源**
   - 投屏激活中，用户点击换源按钮
   - 预期：
     - 投屏自动停止
     - `CastSession` 清理
     - 新源加载，投屏按钮恢复可用

2. **投屏中切换作品**
   - 投屏激活中，返回首页选择新作品
   - 预期：同上

### 6.3 端到端测试（真实 LAN 投屏模拟）

#### 6.3.1 真实电视模拟器（可选高级方案）

若条件允许，建立本地 UPnP 模拟器：

```python
# tests/fixtures/fake_dlna_renderer.py
class FakeDlnaRenderer:
    """模拟 MediaRenderer 设备，响应 SSDP 和 SOAP 请求"""
    
    def __init__(self, port=9090):
        self.port = port
        self.current_uri = None
        self.transport_state = 'STOPPED'
        self.position = 0
        self.duration = 0
    
    def start(self):
        """启动 HTTP 服务器，监听 SSDP 请求"""
        ...
    
    def _handle_set_av_transport_uri(self, uri, metadata):
        """模拟 SetAVTransportURI SOAP 请求"""
        self.current_uri = uri
        self.transport_state = 'STOPPED'
    
    def _handle_play(self):
        """模拟 Play SOAP 请求"""
        self.transport_state = 'PLAYING'
    
    def _handle_get_transport_info(self):
        """模拟 GetTransportInfo SOAP 响应"""
        return {'CurrentTransportState': self.transport_state}
```

#### 6.3.2 端到端测试流程

**测试用例**：

1. **dm84 mp4 投屏**
   - 启动假电视，设置状态为 STOPPED
   - App 启动，选择投屏源、集
   - 验证：
     - SetAVTransportURI 收到正确的 URI（LAN base）
     - 元数据包含集标题
     - Play 被调用后，假电视状态变 PLAYING
     - 轮询获得 PLAYING，本地 VLC 停止

2. **omofuna HLS 投屏**
   - 投屏 HLS 流（.m3u8）
   - 假电视模拟分片请求
   - 验证：
     - 分片 URL 使用 LAN base
     - m3u8 重写正确

3. **电视遥控器播放/暂停**
   - 投屏中，假电视模拟状态变 PAUSED_PLAYBACK
   - 轮询更新
   - 验证：App 暂停按钮启用，播放按钮禁用

4. **电视遥控器 Seek**
   - 假电视模拟 `RelTime` 变化（模拟用户拖条）
   - 验证：App 进度条随之更新

5. **网络中断模拟**
   - 投屏中，假电视停止响应 SOAP 请求
   - 轮询失败 3 次
   - 验证：App 显示「投屏失败」，提示重试

6. **防火墙阻止模拟**
   - LAN 端点绑定失败（socket 异常）
   - 验证：用户提示「Windows 防火墙阻止了投屏端口」

---

## 7. 实现计划

### 7.1 分步骤实现顺序

#### 第一阶段：基础设施（预期 2-3 天）

**步骤 1.1**：投屏专用 LAN 代理端点
- 在 `MediaProxy` 中实现 `start_lan_endpoint()` 和 `stop_lan_endpoint()`
- 实现 `public_base` 参数在 `build_url()` 中的处理
- 测试：本地 URL 和投屏 URL 隔离、token 分组

**步骤 1.2**：DLNA 协议模块
- 实现 `framework/dlna.py` 中的 SSDP 设备发现
- 实现 SOAP 客户端和各控制方法
- 单元测试：协议解析、错误处理

**步骤 1.3**：CastSession 和状态管理
- 设计投屏会话对象（包含设备、轮询 worker、token）
- 实现轮询 worker 类和 Qt 信号
- 测试：状态转移、信号发射、清理逻辑

#### 第二阶段：UI 与交互（预期 2-3 天）

**步骤 2.1**：播放控制条投屏按钮
- 在 `video_view.py:600` 附近添加投屏按钮
- 实现按钮启用/禁用逻辑、状态图标切换
- 连接 `_on_cast_clicked()` 槽函数

**步骤 2.2**：投屏设备选择对话框
- 创建 `gui/components/cast_dialog.py`
- 实现设备列表、刷新、选择逻辑
- 集成 SSDP 扫描

**步骤 2.3**：投屏状态展示区域
- 在播放界面添加状态消息区
- 实现状态变化的文案、进度条、时间标签
- 错误提示和恢复按钮

#### 第三阶段：投屏启动与本地 VLC 关闭（预期 2 天）

**步骤 3.1**：投屏启动流程
- 实现 `_on_cast_clicked()` 的完整流程：扫描 → 选择 → 获取流 → 启动 LAN 端点 → SetURI → Play
- 集成 `_FetchStreamTask` 获取真实流地址

**步骤 3.2**：轮询等待 PLAYING
- 实现 `_wait_for_playing()` 函数
- 8 秒超时逻辑
- 投屏成功时调用 `stop_local_vlc()`

**步骤 3.3**：投屏会话清理
- 实现 `_cleanup_cast_session()` 函数
- 按阶段清理 token、端点、worker
- 测试：资源完全释放

#### 第四阶段：双向控制与状态同步（预期 2 天）

**步骤 4.1**：App 控制路由
- 在播放/暂停/进度/上一集/下一集处添加 `if cast_active` 分支
- 实现 DlnaClient 方法的调用

**步骤 4.2**：轮询状态同步
- 实现轮询 worker 的完整逻辑
- 连接 `state_changed` 信号到 UI 槽函数
- 实现进度条、时间标签、按钮状态的同步更新

**步骤 4.3**：双向兼容处理
- 实现下一集的 `NextAVTransportURI` 支持检测
- 实现 Track 字段的集索引映射
- 错误恢复和提示

#### 第五阶段：测试与调试（预期 2-3 天）

**步骤 5.1**：单元测试完成
- 完成 `test_dlna.py`、`test_media_proxy_lan.py`、`test_video_cast.py`
- 覆盖率 > 80%

**步骤 5.2**：集成测试与 mock
- 构建 CastSession mock 和假电视模拟器
- 运行集成测试场景

**步骤 5.3**：端到端测试（可选）
- 若有真实 MediaRenderer 设备，进行完整功能验收
- 否则使用模拟器进行功能演示

**步骤 5.4**：边界情况与异常
- 投屏中网络中断
- 防火墙、不支持 HLS 的电视
- 快速切集、停止投屏的异步竞态

### 7.2 预期工作量

| 阶段 | 步骤 | 预期工时 | 依赖 |
|-----|------|--------|------|
| 一 | 1.1 | 1 天 | - |
| 一 | 1.2 | 1.5 天 | - |
| 一 | 1.3 | 0.5 天 | 1.1, 1.2 |
| 二 | 2.1 | 0.5 天 | 1.3 |
| 二 | 2.2 | 0.5 天 | 1.2 |
| 二 | 2.3 | 0.5 天 | 2.1, 2.2 |
| 三 | 3.1 | 1 天 | 1.1, 1.2, 2.3 |
| 三 | 3.2 | 0.5 天 | 3.1 |
| 三 | 3.3 | 0.5 天 | 3.2 |
| 四 | 4.1 | 1 天 | 1.2, 3.1 |
| 四 | 4.2 | 1 天 | 1.3, 3.1 |
| 四 | 4.3 | 0.5 天 | 4.2 |
| 五 | 5.1, 5.2 | 1.5 天 | 3.3, 4.3 |
| 五 | 5.3, 5.4 | 1 天 | 5.2 |
| **总计** | | **11-12 天** | |

### 7.3 风险与缓解

| 风险 | 影响 | 缓解方案 |
|-----|------|--------|
| DLNA 设备兼容性差异 | 某些电视 SOAP 错误码不标准 | 构建多厂商模拟器，宽松错误处理 |
| 局域网 IP 获取失败 | 投屏端点无法启动 | 提前检测网络配置，提示用户 |
| 轮询延迟与 UI 卡顿 | 主线程阻塞 | 所有网络操作在独立 worker，只用信号 |
| 本地 VLC 与投屏冲突 | 用户困惑、资源泄漏 | 严格时序控制、单元测试验证 |
| 集切换中断 | 黑屏或无声 | 上一集/下一集缓存策略、保留当前集 |

### 7.4 验收标准

- [ ] 投屏按钮在控制条可见，启用/禁用逻辑正确
- [ ] SSDP 扫描能发现同一 LAN 的 MediaRenderer 设备
- [ ] 选择设备后，SetAVTransportURI 和 Play 成功发送
- [ ] 轮询得到 PLAYING 后，本地 VLC 停止，投屏激活
- [ ] App 播放/暂停/Seek/切集按钮路由到 DlnaClient
- [ ] 电视遥控器操作时，App 进度条和按钮状态同步
- [ ] 网络中断时，3 次轮询失败后显示错误提示
- [ ] 停止投屏时，所有资源正确清理
- [ ] mp4 和 HLS 两种格式投屏成功
- [ ] 单元、集成测试覆盖率 > 80%

---

## 附录：关键数据结构与接口

### A.1 CastSession 对象

```python
@dataclass
class CastSession:
    device: DlnaRenderer  # 目标设备
    dlna_client: DlnaClient  # SOAP 客户端
    poll_worker: CastPollWorker  # 轮询 worker
    lan_endpoint: str  # http://192.168.x.x:port
    cast_token_set: set[str]  # 投屏端点 token
    current_uri: str  # 当前电视播放 URI
    cast_state: str  # idle, scanning, preparing_stream, connecting, playing, paused, stopped, error
    error_msg: str | None  # 错误提示
    created_at: datetime  # 创建时间
    
    def cleanup(self):
        """清理会话资源"""
        ...
```

### A.2 轮询信号

```python
# 自定义 Qt 信号
cast_state_changed = Signal(dict, dict)  # transport_info, position_info
cast_poll_failed = Signal(str)  # error_msg
cast_device_disconnected = Signal()
```

### A.3 转移矩阵（状态机）

| 当前状态 | 事件 | 目标状态 | 触发方 |
|---------|------|--------|-------|
| idle | 点击投屏 | scanning | UI 按钮 |
| scanning | 扫描完成 | idle（无设备）或显示对话框 | SSDP worker |
| idle | 选择设备 | preparing_stream | 用户对话框 |
| preparing_stream | 流获取成功 | connecting | _FetchStreamTask |
| preparing_stream | 流获取失败 | error | _FetchStreamTask |
| connecting | SetURI + Play 成功 | 轮询等待 | DlnaClient |
| 轮询等待 | 收到 PLAYING | playing（cast_active=True） | 轮询 worker |
| 轮询等待 | 8 秒超时 | error | 轮询 timer |
| playing | 遥控器暂停 | paused | 轮询 worker |
| paused | 遥控器播放 | playing | 轮询 worker |
| playing/paused | 点击停止 | idle | UI 按钮 |
| 任何 | 轮询失败 3 次 | error | 轮询 worker |
| error | 点击重试 | scanning 或 preparing_stream | UI 按钮 |

---

## 总结

本设计文档详细规划了 Task 5 中播放界面投屏接入与双向控制的全面需求，包括：

1. **投屏按钮**：位置、图标、状态、禁用条件清晰
2. **状态展示**：分层展示设备、集、进度、消息、错误
3. **控制路由**：`cast_active` 分支决策，五个主要操作的委托逻辑
4. **状态同步**：1 秒轮询周期、进度条实时反映、按钮状态同步
5. **VLC 关闭**：严格的时序控制，确保电视确认后才停本地 VLC
6. **测试策略**：单元、集成、端到端三层，覆盖协议、代理、UI、异常场景
7. **实现计划**：5 个阶段，11-12 天总工时，风险识别与缓解

所有设计遵循现有架构、非目标范围清晰、验收标准可量化。
