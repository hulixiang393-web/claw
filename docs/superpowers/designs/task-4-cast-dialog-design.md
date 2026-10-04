# Task 4：投屏设备选择对话框 - 详细设计文档

- **日期**：2026-10-03
- **状态**：设计阶段（非实现）
- **关联任务**：DLNA 投屏集成
- **前置条件**：Task 1-3 完成（DLNA 模块、媒体代理局域网端点、投屏会话管理）

---

## 目录

1. [需求概述](#需求概述)
2. [对话框架构](#对话框架构)
3. [SSDP 扫描 UX](#ssdp-扫描-ux)
4. [与 CastSession 的接入点](#与-castsession-的接入点)
5. [测试策略](#测试策略)
6. [实现计划](#实现计划)

---

## 需求概述

### 功能目标

设计一个模态对话框，供用户在投屏流程中选择目标 MediaRenderer 设备。对话框需要：

1. **发现设备**：通过 SSDP M-SEARCH 扫描局域网设备。
2. **展示列表**：显示设备 friendly name 和 IP 地址，便于用户识别。
3. **选择与确认**：用户选中一台设备后返回 `DlnaRenderer` 对象给投屏流程。
4. **错误反馈**：清晰提示网络问题、防火墙阻止、无设备等异常。
5. **重试机制**：用户可重新扫描，刷新列表。

### 设计约束

- 沿用现有 `source_select_dialog.py` 的组件风格和交互模式。
- 不涉及复杂的设备过滤或排序；优先展示所有发现的设备。
- SSDP 扫描由后台线程执行，防止 UI 阻塞。
- 对话框关闭时应清理扫描任务。
- 当前集标题需在对话框顶部显示以增加上下文感知。

---

## 对话框架构

### 2.1 组件树结构

```
CastDeviceDialog (QDialog)
├─ QVBoxLayout (main_layout)
│
├─ [标题区]
│  └─ QLabel: "选择投屏设备"
│
├─ [当前集信息区]
│  ├─ QLabel: "正在投屏："
│  └─ QLabel (current_title_label): 当前集标题 (动态更新)
│
├─ [扫描进度与控件区]
│  └─ QHBoxLayout
│     ├─ QLabel (scan_status_label): "正在扫描..." / "找到 X 台设备" / "扫描超时"
│     ├─ QProgressBar (scan_progress): 扫描进度条
│     └─ QPushButton (refresh_btn): "刷新"
│
├─ [设备列表区]
│  └─ QListWidget (device_list)
│     └─ QListWidgetItem × N (动态添加)
│        ├─ item_text: "电视名称 (192.168.1.100)"
│        └─ item_data: DlnaRenderer 对象
│
├─ [空列表提示]
│  └─ QLabel (empty_hint_label)
│     text: "请确认电脑和电视处于同一局域网，并允许应用通过 Windows 防火墙"
│     visible: True only when device_list is empty
│
├─ [错误区]
│  └─ QLabel (error_label)
│     text: 错误信息 (网络错误、防火墙、超时等)
│     style: 红色字体
│
├─ [分隔线]
│  └─ QFrame (QFrame.HLine)
│
├─ [按钮区]
│  └─ QHBoxLayout
│     ├─ QSpacerItem (水平弹簧)
│     ├─ QPushButton (cancel_btn): "取消"
│     └─ QPushButton (select_btn): "确定"
│        enabled: False (直到选中设备)
```

### 2.2 信号与槽连接

```python
# 对话框内部信号-槽连接
scan_worker.device_discovered.connect(on_device_discovered)
  └─ 新设备发现时追加到列表

scan_worker.scan_complete.connect(on_scan_complete)
  └─ 扫描完成时更新状态标签、隐藏进度条

scan_worker.scan_error.connect(on_scan_error)
  └─ 错误发生时显示错误提示、禁用刷新按钮

refresh_btn.clicked.connect(on_refresh_clicked)
  └─ 清空列表、重新启动扫描

cancel_btn.clicked.connect(reject)
  └─ 取消对话框

select_btn.clicked.connect(on_select_device)
  └─ 确认选择，返回 QDialog.Accepted

device_list.itemSelectionChanged.connect(on_device_selected)
  └─ 更新 select_btn 启用状态

device_list.itemDoubleClicked.connect(on_device_double_clicked)
  └─ 双击设备直接确认选择（快捷操作）
```

### 2.3 数据模型

#### 核心数据结构

```python
# 从 framework/dlna.py 继承
@dataclass(frozen=True)
class DlnaRenderer:
    udn: str                              # 唯一设备号 (去重依据)
    friendly_name: str                    # 用户可读的电视名称
    location: str                         # Device description XML 位置
    av_transport_control_url: str         # AVTransport 控制 URL
    rendering_control_url: str | None     # RenderingControl URL (可选)
    service_type: str                     # DLNA service type

# 对话框内部状态
class CastDeviceDialog:
    _devices: dict[str, DlnaRenderer]     # UDN → DlnaRenderer 映射 (去重)
    _scan_worker: SsdpScanWorker | None   # 后台扫描任务
    _selected_device: DlnaRenderer | None # 当前选中设备
    _current_episode_title: str           # 当前集标题 (传入参数)
```

#### 设备列表缓存策略

> **修订（2026-10-04）**：原设计为「刷新保留 `_devices` 缓存、对话框间 QSettings 持久化」，
> 与实现（`gui/components/cast_dialog.py`）相反——保留缓存会让上一轮已发现的设备
> 在后续扫描中因 UDN 去重而永不再现。现按实现修订如下。

```
扫描流程中的去重与回填机制（与实现一致）：

1. 对话框打开时：
   - 初始化 _devices = {}（不加载历史缓存；暂不做 QSettings 持久化）
   - 立即启动扫描（不显示灰显缓存）

2. 扫描完成时：
   - 后台线程一次性返回 list[DlnaRenderer]，主线程按 UDN 去重回填：
     UDN 不在 _devices 中 → _devices[udn] = renderer 并追加列表项
     UDN 已存在 → 跳过（不重复添加）

3. 用户点击「刷新」：
   - 清空列表视图、清空 _devices 去重缓存、清空选中状态并禁用确定按钮
   - 重新启动扫描，本轮结果重新按 UDN 去重回填
   - 离线设备因缓存已清空而自然不再显示

策略取舍：
- 刷新清空缓存符合「刷新即重新发现」语义，避免已下线设备长期占位
- 扫描失败时列表保持为空，显示错误提示并重新启用刷新按钮供重试
- QSettings 跨对话框持久化暂不实现，留作后续优化
```

### 2.4 刷新机制

#### 对话框生命周期内的更新流程

```
user_open_dialog()
  │
  ├─ __init__: 初始化 UI 组件
  │
  ├─ showEvent():
  │   ├─ 直接启动 SsdpScanWorker (timeout=2.0 秒)——_devices 初始为空，
  │   │   不加载/不显示历史缓存（修订 2026-10-04：原「显示缓存设备（disabled）」不实现）
  │   └─ 更新状态为 "正在扫描..."
  │
  ├─ 扫描过程 (后台线程):
  │   ├─ 每发现新设备
  │   │   └─ device_discovered signal → on_device_discovered()
  │   │       ├─ 追加到 _devices[udn]
  │   │       ├─ 追加 QListWidgetItem 到 device_list
  │   │       ├─ 更新 scan_status_label ("找到 X 台设备")
  │   │       └─ 隐藏 empty_hint_label
  │   │
  │   └─ 扫描超时 (2.0 秒)
  │       └─ scan_complete signal → on_scan_complete()
  │           ├─ 停止进度条动画
  │           ├─ 更新状态 "扫描完成，找到 X 台设备" 或 "未找到设备"
  │           ├─ 启用 refresh_btn
  │           └─ 若设备为空，显示 empty_hint_label
  │
  ├─ 用户交互:
  │   ├─ 点击「刷新」:
  │   │   ├─ 禁用 refresh_btn
  │   │   ├─ 清空 device_list 视图
  │   │   ├─ 清空 _devices 去重缓存、清空选中并禁用确定按钮
  │   │   │   （修订 2026-10-04：原「保留 _devices 字典（作为缓存）」与实现相反）
  │   │   ├─ 重新启动 SsdpScanWorker
  │   │   └─ 循环回到扫描过程
  │   │
  │   ├─ 选择设备:
  │   │   └─ on_device_selected()
  │   │       ├─ _selected_device = 选中的 DlnaRenderer
  │   │       └─ 启用 select_btn
  │   │
  │   ├─ 双击设备:
  │   │   └─ on_device_double_clicked()
  │   │       └─ 直接返回 QDialog.Accepted (跳过点击按钮)
  │   │
  │   └─ 点击「确定」:
  │       └─ accept() → 返回 _selected_device
  │
  └─ closeEvent() / reject():
      └─ 清理 _scan_worker (停止扫描线程)

在此过程中，UI 更新总是通过 Qt signal 从后台线程转发到主线程，
确保线程安全。
```

---

## SSDP 扫描 UX

### 3.1 超时设置

```python
class SsdpScanWorker(QRunnable):
    """后台 SSDP 扫描任务"""

    SSDP_TIMEOUT = 2.0  # 秒，固定超时
    SSDP_MULTICAST_ADDR = "239.255.255.250"
    SSDP_MULTICAST_PORT = 1900

    # 扫描阶段：
    # Phase 1 (0.3s): M-SEARCH ST: urn:schemas-upnp-org:device:MediaRenderer:1
    # Phase 2 (0.3s): M-SEARCH ST: upnp:rootdevice (兜底)
    # Phase 3 (1.4s): 接收响应、并行请求 location URL、解析 XML
    #
    # 总计 2.0 秒，确保大多数设备能在第一阶段被发现，
    # 同时给响应缓慢的设备第二次机会。
```

**设计理由**：
- 2.0 秒是业界标准（UPnP 规范建议 1-5 秒）
- 过短（< 1.0s）：丢失响应缓慢的设备
- 过长（> 3.0s）：用户等待感受差
- 分两阶段查询确保广义设备兼容性

### 3.2 进度提示

```
进度条与文本提示同步：

时间轴:
  0%     ├─ "正在扫描..." (扫描开始)
  50%    ├─ 首批设备显示时，进度条跳至 50%
  100%   └─ 扫描超时或主动停止，进度条填满

状态标签变化:
  "正在扫描..."
    ↓ (发现第一台设备)
  "找到 1 台设备..."
    ↓ (继续扫描中)
  "找到 X 台设备..."
    ↓ (超时或完成)
  "扫描完成，找到 X 台设备" 或 "未找到设备"

UI 提示：
  - 进度条始终可见，用户能实时感知扫描进度
  - 列表实时更新，不等到扫描完成再显示（增强响应感）
  - 列表项不可选中直到扫描完成（防止用户选中"正在加载"的设备）
```

### 3.3 设备列表缓存

#### 缓存策略详解

> **修订（2026-10-04）**：本节原为「灰显历史缓存 + 刷新保留 `_devices` + QSettings 持久化」，
> 与实现相反，现按 `gui/components/cast_dialog.py` 实际行为重写。

```python
class CastDeviceDialog(QDialog):

    def __init__(self, current_episode_title: str, parent=None):
        """每次打开均从空缓存开始；暂不做 QSettings 持久化"""
        self._devices = {}  # 仅用于本次扫描内的 UDN 去重，不是跨轮缓存

    def showEvent(self, event):
        """对话框显示时的初始化"""
        super().showEvent(event)

        # 不显示历史缓存（_devices 初始为空），直接启动扫描
        self._start_scan()

    def _start_scan(self):
        """启动后台扫描线程"""
        self._scan_worker = SsdpScanWorker()
        self._scan_worker.device_discovered.connect(self._on_device_discovered)
        self._scan_worker.scan_complete.connect(self._on_scan_complete)
        self._scan_worker.scan_error.connect(self._on_scan_error)

        thread_pool = QThreadPool.globalInstance()
        thread_pool.start(self._scan_worker)

    def _on_device_discovered(self, renderer: DlnaRenderer):
        """发现新设备时调用（可能多次）"""

        # 去重：同一 UDN 不重复添加
        if renderer.udn in self._devices:
            return  # 已有此设备，跳过

        self._devices[renderer.udn] = renderer

        # 追加到列表视图
        item_text = f"{renderer.friendly_name} ({renderer.location.split('//')[1].split(':')[0]})"
        item = QListWidgetItem(item_text)
        item.setData(Qt.UserRole, renderer.udn)  # 存储 UDN 便于查询

        # 前缀图标表示"在线"
        item.setIcon(self._get_device_icon())  # 小电视图标

        self.device_list.addItem(item)

        # 更新状态标签
        device_count = len(self._devices)
        self.scan_status_label.setText(f"找到 {device_count} 台设备...")

        # 隐藏空列表提示
        self.empty_hint_label.setVisible(False)

        # 列表项暂不可选（直到扫描完成）
        item.setFlags(item.flags() & ~Qt.ItemIsSelectable)

    def _on_scan_complete(self):
        """扫描超时或主动完成时调用"""

        # 启用列表项选择
        for i in range(self.device_list.count()):
            item = self.device_list.item(i)
            item.setFlags(item.flags() | Qt.ItemIsSelectable)

        # 更新进度条
        self.scan_progress.setValue(100)

        # 更新状态标签
        device_count = len(self._devices)
        if device_count == 0:
            self.scan_status_label.setText("未找到设备（请检查网络）")
            self.empty_hint_label.setVisible(True)
        else:
            self.scan_status_label.setText(f"扫描完成，找到 {device_count} 台设备")

        # 启用刷新按钮
        self.refresh_btn.setEnabled(True)

        # QSettings 持久化暂不实现（修订 2026-10-04）：
        # 不保存本轮扫描结果到配置，下次打开重新扫描

    def on_refresh_clicked(self):
        """用户点击「刷新」按钮"""

        # 禁用刷新按钮防止多次点击
        self.refresh_btn.setEnabled(False)

        # 刷新即清空：列表视图 + _devices 去重缓存 + 选中状态
        # （修订 2026-10-04：原「保留 _devices 作为缓存」与实现相反，
        #   保留缓存会让已下线设备因去重而永不再现）
        self._devices.clear()
        self._selected = None
        self.device_list.clear()
        self.select_btn.setEnabled(False)

        # 重置进度条
        self.scan_progress.setValue(0)
        self.scan_status_label.setText("正在重新扫描...")

        # 重新启动扫描，本轮结果按 UDN 去重新回填
        self._start_scan()
```

**修订后的策略说明（2026-10-04，与实现一致）**：
1. `_devices` 只承担「单次扫描内的 UDN 去重」职责，刷新即清空。
2. 扫描失败时列表为空：显示错误提示、重新启用刷新按钮供重试（无缓存兜底）。
3. 无跨对话框持久化：关闭后重开需重新扫描（QSettings 缓存留作后续优化）。

### 3.4 错误提示机制

#### 错误分类与提示文案

```python
class ScanError(Enum):
    """SSDP 扫描可能的错误"""

    NO_NETWORK = "no_network"
    # 文案：
    # "网络连接异常。请检查以下情况：\n"
    # "1. 电脑已连接到局域网\n"
    # "2. 电视与电脑在同一网络\n"
    # "3. 路由器允许多播流量"

    FIREWALL_BLOCKED = "firewall_blocked"
    # 文案：
    # "Windows 防火墙可能阻止了应用。请：\n"
    # "1. 打开 Windows Defender 防火墙\n"
    # "2. 点击「允许应用通过防火墙」\n"
    # "3. 确保此应用已勾选"私有网络"和"公用网络"\n"
    # "4. 点击「刷新」重试"

    SOCKET_ERROR = "socket_error"
    # 文案：
    # "套接字配置失败（通常是网络驱动问题）。\n"
    # "请重启电脑或尝试连接不同网络。"

    DEVICE_XML_PARSE_ERROR = "device_parse_error"
    # 文案：
    # "某台设备的信息无法解析（设备固件问题）。\n"
    # "其他设备可能仍然可用。"

    SCAN_TIMEOUT = "scan_timeout"  # 不显示为错误，而是正常完成
    # 文案：
    # "扫描超时（未发现设备）。\n"
    # "请检查设备是否在线。"

    UNKNOWN = "unknown"
    # 文案：
    # "扫描失败，原因未知。请查看日志或重试。"


class CastDeviceDialog(QDialog):

    def _on_scan_error(self, error_type: str, error_msg: str):
        """后台扫描发生错误时调用"""

        # 解析错误类型
        error_enum = ScanError(error_type)

        # 根据错误类型生成用户友好的提示
        if error_enum == ScanError.NO_NETWORK:
            user_message = (
                "网络连接异常。请检查以下情况：\n"
                "1. 电脑已连接到局域网\n"
                "2. 电视与电脑在同一网络\n"
                "3. 路由器允许多播流量"
            )
        elif error_enum == ScanError.FIREWALL_BLOCKED:
            user_message = (
                "Windows 防火墙可能阻止了应用。请：\n"
                "1. 打开 Windows Defender 防火墙\n"
                "2. 点击「允许应用通过防火墙」\n"
                "3. 确保此应用已勾选"私有网络"和"公用网络"\n"
                "4. 点击「刷新」重试"
            )
        elif error_enum == ScanError.SOCKET_ERROR:
            user_message = (
                "套接字配置失败（通常是网络驱动问题）。\n"
                "请重启电脑或尝试连接不同网络。"
            )
        elif error_enum == ScanError.DEVICE_XML_PARSE_ERROR:
            user_message = (
                "某台设备的信息无法解析（设备固件问题）。\n"
                "其他设备可能仍然可用。"
            )
        else:
            user_message = f"扫描失败：{error_msg}"

        # 显示错误标签
        self.error_label.setText(user_message)
        self.error_label.setVisible(True)

        # 记录日志（不包含完整错误堆栈，仅记录错误类型）
        logger.warning(f"SSDP scan error: {error_type}")

        # 更新状态标签
        self.scan_status_label.setText("扫描出错，请重试")

        # 启用刷新按钮允许重试
        self.refresh_btn.setEnabled(True)

        # 列表为空则显示空列表提示（无缓存兜底，修订 2026-10-04）
        if len(self._devices) == 0:
            self.empty_hint_label.setVisible(True)


def _show_no_devices_hint():
    """当列表为空时显示提示"""
    hint_text = (
        "请确认电脑和电视处于同一局域网，"
        "并允许应用通过 Windows 防火墙。\n\n"
        "如已确认网络配置正确，请：\n"
        "1. 检查电视是否开启\n"
        "2. 检查电视 DLNA 功能是否启用\n"
        "3. 尝试重新启动路由器"
    )
    self.empty_hint_label.setText(hint_text)
    self.empty_hint_label.setVisible(True)
```

---

## 与 CastSession 的接入点

### 4.1 对话框返回值与类型

```python
# 对话框返回值：
result = cast_dialog.exec()

if result == QDialog.Accepted:
    # 用户选中设备
    selected_renderer: DlnaRenderer = cast_dialog.selected_device()

    # selected_renderer 包含：
    # - udn: 设备唯一标识
    # - friendly_name: 用户可读的设备名
    # - location: Device description XML 位置
    # - av_transport_control_url: AVTransport 服务 URL
    # - rendering_control_url: RenderingControl 服务 URL (可选)
    # - service_type: DLNA service type

    # 将其传递给投屏流程
    self._start_casting(selected_renderer)

elif result == QDialog.Rejected:
    # 用户取消，保留本地播放
    pass
```

### 4.2 投屏流程中的集成点

```python
# 在 video_view.py 中，投屏入口点

class VideoView(QWidget):

    def _on_cast_clicked(self):
        """用户点击投屏按钮"""

        # 第一步：获取当前集信息
        current_episode_title = self._get_current_episode_title()

        # 第二步：打开设备选择对话框
        from gui.components.cast_dialog import CastDeviceDialog

        dialog = CastDeviceDialog(current_episode_title, parent=self)

        # 第三步：等待用户选择
        result = dialog.exec()

        if result != QDialog.Accepted:
            # 用户取消
            return

        # 第四步：获取选中的设备
        selected_renderer: DlnaRenderer = dialog.selected_device()

        # 第五步：创建投屏会话
        cast_session = CastSession(
            renderer=selected_renderer,
            media_proxy=self._media_proxy,
            current_idx=self._current_idx,
        )

        # 第六步：启动投屏
        self._start_casting_workflow(cast_session)

    def _start_casting_workflow(self, cast_session: CastSession):
        """启动投屏的完整流程"""

        # 此流程在 Task 3 中已定义，此处仅作调用示意

        try:
            # 6.1 获取当前集真实流地址
            stream_url = self._fetch_stream_url(self._current_idx)

            # 6.2 启动局域网代理端点
            renderer_ip = cast_session.renderer.location.split("//")[1].split(":")[0]
            local_ip = self._get_local_ip_for_renderer(renderer_ip)

            lan_base = self._media_proxy.start_lan_endpoint(bind_host=local_ip)

            # 6.3 生成投屏 URL（指向局域网端点）
            cast_url = self._media_proxy.build_url(
                stream_url,
                public_base=lan_base,
            )

            # 6.4 向电视下发 SetAVTransportURI + Play
            dlna_client = DlnaClient(cast_session.renderer)
            dlna_client.set_av_transport_uri(
                cast_url,
                title=self._get_current_episode_title(),
                mime_type="application/vnd.apple.mpegurl",
            )
            dlna_client.play()

            # 6.5 启动轮询 worker 等待 PLAYING
            self._cast_poll_worker = CastPollWorker(dlna_client)
            self._cast_poll_worker.state_changed.connect(self._on_cast_state_changed)

            # 等待 8 秒
            for i in range(8):
                time.sleep(1)
                if self._cast_poll_worker.current_state == "PLAYING":
                    break

            if self._cast_poll_worker.current_state == "PLAYING":
                # 6.6 电视开始播放，停止本地 VLC
                self._stop_local_player()
                self._cast_active = True
            else:
                # 电视未进入 PLAYING 状态，恢复本地播放
                self._media_proxy.stop_lan_endpoint()
                self._show_error("投屏失败，电视未响应")

        except Exception as e:
            # 任何错误都保留本地播放
            self._media_proxy.stop_lan_endpoint()
            self._show_error(f"投屏启动失败：{e}")
```

### 4.3 对话框到 CastSession 的数据流

```
┌─────────────────────────────────────────────────────────────────┐
│ CastDeviceDialog                                                │
│                                                                  │
│  [SSDP 扫描]                                                     │
│  discover_renderers() → list[DlnaRenderer]                      │
│           ↓                                                      │
│  [显示列表 + 选择]                                                │
│  user selects device → _selected_device                         │
│           ↓                                                      │
│  dialog.exec() → QDialog.Accepted                               │
│           ↓                                                      │
│  selected_renderer = dialog.selected_device()                   │
│           ↓                                                      │
└─────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│ video_view._start_casting_workflow()                            │
│                                                                  │
│  CastSession(                                                   │
│      renderer=selected_renderer,                                │
│      media_proxy=self._media_proxy,                             │
│      current_idx=self._current_idx,                             │
│  )                                                              │
│           ↓                                                      │
│  [获取流 URL、启动代理端点、下发 SetAVTransportURI]               │
│           ↓                                                      │
│  DlnaClient.set_av_transport_uri(lan_url, metadata)             │
│           ↓                                                      │
│  等待 PLAYING → 停止本地 VLC                                     │
│           ↓                                                      │
└─────────────────────────────────────────────────────────────────┘
                           ↓
           投屏会话激活：App 开始控制电视
```

---

## 测试策略

### 5.1 单元测试

#### 5.1.1 模型与数据部分

**测试文件**：`tests/test_cast_dialog_model.py`

```python
def test_dlna_renderer_dataclass():
    """测试 DlnaRenderer 数据类初始化和去重逻辑"""
    r1 = DlnaRenderer(
        udn="uuid:device-1",
        friendly_name="客厅电视",
        location="http://192.168.1.100:49152/description.xml",
        av_transport_control_url="/control/av-transport",
        rendering_control_url="/control/rendering",
        service_type="urn:schemas-upnp-org:service:AVTransport:1",
    )

    r2 = DlnaRenderer(udn="uuid:device-1", ...)  # 同 UDN

    assert r1 == r2  # frozen dataclass 支持直接比较
    assert r1.udn == r2.udn
```

**测试文件**：`tests/test_cast_dialog_device_cache.py`

```python
def test_device_list_deduplication():
    """测试设备去重逻辑"""
    devices = {}

    # 模拟发现同一 UDN 的设备两次
    r1 = make_test_renderer(udn="uuid:tv-1", friendly_name="TV 1")
    r2 = make_test_renderer(udn="uuid:tv-1", friendly_name="TV 1 (updated)")

    devices["uuid:tv-1"] = r1
    devices["uuid:tv-1"] = r2  # 覆盖

    assert len(devices) == 1
    assert devices["uuid:tv-1"].friendly_name == "TV 1 (updated)"


# 修订（2026-10-04）：原 test_cache_persistence（QSettings 保存/加载往返）已取消——
# QSettings 持久化暂不实现，跨对话框缓存留作后续优化。
# 「刷新清空去重缓存」的断言见 5.1.3 的 test_refresh_clears_list_and_rescans。
```

#### 5.1.2 UI 信号与槽

**测试文件**：`tests/test_cast_dialog_signals.py`

```python
def test_device_discovered_signal():
    """测试设备发现信号正确发送和接收"""
    dialog = CastDeviceDialog("Episode 1")
    signal_spy = QSignalSpy(dialog._scan_worker.device_discovered)

    renderer = make_test_renderer(udn="uuid:tv-1")
    dialog._scan_worker.device_discovered.emit(renderer)

    assert signal_spy.count() == 1
    assert dialog.device_list.count() == 1


def test_device_selected_enables_button():
    """测试选中设备后启用确定按钮"""
    dialog = CastDeviceDialog("Episode 1")

    # 添加一个测试设备到列表
    item = QListWidgetItem("TV 1 (192.168.1.100)")
    dialog.device_list.addItem(item)

    # 初始状态：select_btn 禁用
    assert not dialog.select_btn.isEnabled()

    # 选中设备
    dialog.device_list.setCurrentItem(item)
    dialog._on_device_selected()

    # select_btn 应启用
    assert dialog.select_btn.isEnabled()


def test_cancel_closes_dialog():
    """测试取消按钮关闭对话框"""
    dialog = CastDeviceDialog("Episode 1")

    # 模拟点击取消按钮
    dialog.cancel_btn.click()

    # 对话框应以 Rejected 状态返回
    assert dialog.result() == QDialog.Rejected


def test_select_device_accepts_dialog():
    """测试确定按钮返回 Accepted 和设备对象"""
    dialog = CastDeviceDialog("Episode 1")

    # 添加并选中设备
    renderer = make_test_renderer(udn="uuid:tv-1")
    dialog._devices["uuid:tv-1"] = renderer

    item = QListWidgetItem("TV 1 (192.168.1.100)")
    item.setData(Qt.UserRole, "uuid:tv-1")
    dialog.device_list.addItem(item)
    dialog.device_list.setCurrentItem(item)
    dialog._on_device_selected()

    # 点击确定
    dialog.select_btn.click()

    # 对话框应以 Accepted 状态返回
    assert dialog.result() == QDialog.Accepted
    assert dialog.selected_device().udn == "uuid:tv-1"
```

#### 5.1.3 扫描流程与错误处理

**测试文件**：`tests/test_cast_dialog_scan.py`

```python
def test_scan_timeout():
    """测试 SSDP 扫描在超时后停止"""
    dialog = CastDeviceDialog("Episode 1")

    # 模拟无设备环境
    with patch('framework.dlna.discover_renderers', return_value=[]):
        dialog._start_scan()

        # 等待扫描超时
        time.sleep(2.5)

        # 验证状态：扫描应已完成
        assert dialog.scan_status_label.text() == "未找到设备（请检查网络）"
        assert dialog.empty_hint_label.isVisible()


def test_scan_error_firewall():
    """测试防火墙错误提示"""
    dialog = CastDeviceDialog("Episode 1")

    # 模拟防火墙错误
    dialog._on_scan_error("firewall_blocked", "Permission denied")

    # 验证错误提示
    assert "防火墙" in dialog.error_label.text()
    assert dialog.error_label.isVisible()
    assert dialog.refresh_btn.isEnabled()  # 允许重试


def test_refresh_clears_list_and_rescans():
    """测试刷新按钮清空列表并重新扫描"""
    dialog = CastDeviceDialog("Episode 1")

    # 添加初始设备
    dialog._devices["uuid:tv-1"] = make_test_renderer(udn="uuid:tv-1")
    item = QListWidgetItem("TV 1")
    dialog.device_list.addItem(item)

    # 点击刷新
    dialog.on_refresh_clicked()

    # 验证：列表与去重缓存同时清空（修订 2026-10-04：原「缓存保留」与实现相反）
    assert dialog.device_list.count() == 0
    assert len(dialog._devices) == 0  # 缓存被清空，本轮扫描重新按 UDN 回填
    assert dialog.scan_status_label.text() == "正在重新扫描..."
```

### 5.2 集成测试

#### 5.2.1 真实 SSDP 替身（Mock）

**测试文件**：`tests/test_cast_dialog_integration.py`

```python
def test_cast_dialog_with_mock_ssdp():
    """集成测试：使用 Mock SSDP 响应测试完整流程"""

    # Mock SSDP discover_renderers 函数
    mock_renderers = [
        make_test_renderer(udn="uuid:tv-1", friendly_name="客厅电视"),
        make_test_renderer(udn="uuid:tv-2", friendly_name="卧室电视"),
    ]

    with patch('framework.dlna.discover_renderers', return_value=mock_renderers):
        dialog = CastDeviceDialog("Episode 1")

        # 显示对话框（触发 showEvent）
        dialog.show()

        # 等待扫描完成
        time.sleep(2.5)

        # 验证：列表应显示 2 台设备
        assert dialog.device_list.count() == 2
        assert "客厅电视" in dialog.device_list.item(0).text()
        assert "卧室电视" in dialog.device_list.item(1).text()

        # 选中第一台设备
        dialog.device_list.setCurrentRow(0)
        dialog._on_device_selected()

        # 点击确定
        result = dialog.exec()

        # 验证：应返回 Accepted，且设备正确
        assert result == QDialog.Accepted
        assert dialog.selected_device().udn == "uuid:tv-1"


def test_cast_dialog_to_cast_session():
    """集成测试：对话框返回值正确传递到 CastSession"""

    mock_renderer = make_test_renderer(udn="uuid:tv-1")

    with patch('framework.dlna.discover_renderers', return_value=[mock_renderer]):
        dialog = CastDeviceDialog("Episode 1")
        dialog.show()

        time.sleep(2.5)  # 等待扫描

        dialog.device_list.setCurrentRow(0)
        dialog._on_device_selected()

        # 模拟点击确定
        selected_renderer = dialog.selected_device()

        # 创建 CastSession
        cast_session = CastSession(
            renderer=selected_renderer,
            media_proxy=mock_media_proxy(),
            current_idx=0,
        )

        # 验证：CastSession 初始化正确
        assert cast_session.renderer.udn == "uuid:tv-1"
        assert cast_session.renderer.friendly_name == "客厅电视"
```

### 5.3 手工验收测试

```
手工验收用例：

1. 正常发现流程
   ├─ 前置：电视开启、处于同一局域网
   ├─ 操作：打开投屏对话框
   ├─ 期望：1-2 秒内列出电视设备
   └─ 验证：设备名和 IP 显示正确

2. 多台设备
   ├─ 前置：同一网络有 3+ 台设备
   ├─ 操作：打开对话框
   ├─ 期望：列表按发现顺序显示所有设备
   └─ 验证：可选择任意设备

3. 无设备场景
   ├─ 前置：网络中无 DLNA 设备
   ├─ 操作：打开对话框
   ├─ 期望：2 秒后显示"未找到设备"，显示帮助提示
   └─ 验证：刷新按钮启用

4. 防火墙阻止
   ├─ 前置：Windows 防火墙阻止应用
   ├─ 操作：打开对话框
   ├─ 期望：显示"防火墙"相关错误提示
   └─ 验证：允许防火墙后刷新可发现

5. 网络断开
   ├─ 前置：电脑连网，打开对话框开始扫描
   ├─ 操作：扫描中拔掉网线
   ├─ 期望：显示网络错误提示
   └─ 验证：重连网络后刷新恢复

6. 选择设备并投屏
   ├─ 前置：列表显示电视
   ├─ 操作：双击或选中后点击确定
   ├─ 期望：对话框关闭，投屏流程启动
   └─ 验证：电视开始播放（后续由 Task 3 验证）

7. 刷新功能
   ├─ 前置：列表已显示设备
   ├─ 操作：点击刷新按钮
   ├─ 期望：列表与 _devices 缓存、选中状态同时清空，重新扫描
   └─ 验证：1-2 秒后设备按 UDN 去重重新显示，无重复项

8. 无跨次缓存（修订 2026-10-04：原「灰显缓存」不实现）
   ├─ 前置：列表显示设备
   ├─ 操作：关闭对话框，立即重新打开
   ├─ 期望：从空列表开始重新扫描，不显示上次结果
   └─ 验证：扫描完成后按 UDN 去重回填，结果与上次一致（不重复）

9. 取消操作
   ├─ 前置：对话框打开
   ├─ 操作：点击取消
   ├─ 期望：对话框关闭，投屏不启动
   └─ 验证：本地播放继续

10. 当前集标题显示
    ├─ 前置：对话框打开，显示当前集信息
    ├─ 期望：标题正确显示在对话框顶部
    └─ 验证：切换集后对话框标题也更新
```

---

## 实现计划

### 6.1 分步骤实现

#### Phase 1：基础组件与 UI 框架（预期 2-3 天）

**任务 1.1**：创建 `gui/components/cast_dialog.py` 基础结构
- [ ] 定义 `CastDeviceDialog` 类，继承 `QDialog`
- [ ] 构建 UI 组件树（标题、列表、按钮、状态标签等）
- [ ] 连接基础信号-槽（取消、确定、刷新按钮点击）
- [ ] 实现 `selected_device()` 方法返回选中的 `DlnaRenderer`
- **工作量**：1 天，复杂度低

**任务 1.2**：定义 `SsdpScanWorker` 后台任务
- [ ] 创建 `SsdpScanWorker(QRunnable)` 类
- [ ] 定义 `device_discovered`、`scan_complete`、`scan_error` 信号
- [ ] 实现 `run()` 方法的基础框架（调用 `discover_renderers()`）
- [ ] 确保线程安全地发送信号
- **工作量**：1 天，复杂度中等

**任务 1.3**：集成 SSDP 发现（依赖 Task 1）
- [ ] 调用 `framework/dlna.py` 的 `discover_renderers()` 函数
- [ ] 处理设备列表，去重并存储到 `_devices` 字典
- [ ] 实现 `_on_device_discovered()` 追加设备到列表
- [ ] 实现 `_on_scan_complete()` 更新 UI 状态
- **工作量**：1 天，复杂度中等

#### Phase 2：交互与刷新（预期 2-3 天）

**任务 2.1**：实现选择与确认逻辑
- [ ] `device_list.itemSelectionChanged` 信号连接
- [ ] 选中时启用 `select_btn`，未选中时禁用
- [ ] 双击设备直接确认（`itemDoubleClicked` 信号）
- [ ] `accept()` 时返回 `QDialog.Accepted` 并保存选择
- **工作量**：0.5 天，复杂度低

**任务 2.2**：设备去重与刷新清理（修订 2026-10-04：原「QSettings 持久化 / 灰显缓存」不做）
- [ ] `_devices` 按 UDN 去重回填扫描结果
- [ ] 刷新时清空 `_devices`、列表与选中状态（不保留缓存）
- [ ] 扫描失败时显示错误提示并重新启用刷新按钮
- **工作量**：1 天，复杂度中等

**任务 2.3**：刷新与重试
- [ ] 实现 `on_refresh_clicked()` 逻辑
- [ ] 清空列表视图与 `_devices` 去重缓存（修订 2026-10-04：原「保留 `_devices` 缓存」与实现相反）
- [ ] 禁用刷新按钮防止多次点击
- [ ] 重新启动扫描
- [ ] 暂停中可取消扫描
- **工作量**：1 天，复杂度低

#### Phase 3：进度提示与错误处理（预期 2-3 天）

**任务 3.1**：进度条与状态标签
- [ ] `QProgressBar` 初始化，0-100 范围
- [ ] 扫描开始时重置进度条
- [ ] 首设备发现时跳至 50%，完成时 100%
- [ ] 实时更新 `scan_status_label` 显示已发现设备数
- **工作量**：0.5 天，复杂度低

**任务 3.2**：错误分类与用户提示
- [ ] 定义 `ScanError` 枚举（网络、防火墙、解析等）
- [ ] `_on_scan_error()` 根据错误类型生成用户友好文案
- [ ] 显示 `error_label` 并保持可见
- [ ] 允许用户点击刷新重试
- **工作量**：1 天，复杂度中等

**任务 3.3**：无设备提示
- [ ] 扫描完成后检查设备数
- [ ] 如为 0，显示 `empty_hint_label` 与帮助文本
- [ ] 提示文案包括网络配置、防火墙、设备检查
- [ ] 用户可点击链接查看详细帮助（可选）
- **工作量**：0.5 天，复杂度低

#### Phase 4：单元测试与集成（预期 2-3 天）

**任务 4.1**：单元测试（数据与模型）
- [ ] 编写 `tests/test_cast_dialog_model.py`
- [ ] 测试 `DlnaRenderer` 去重
- [ ] 测试刷新清空 `_devices` 去重缓存（修订 2026-10-04：原「缓存加载/保存」不做）
- [ ] 测试 URL 解析和 IP 提取
- **工作量**：1 天，复杂度低

**任务 4.2**：信号与槽单元测试
- [ ] 编写 `tests/test_cast_dialog_signals.py`
- [ ] 测试设备发现信号
- [ ] 测试按钮点击和状态变化
- [ ] 测试选中状态的 UI 反应
- **工作量**：1 天，复杂度低

**任务 4.3**：集成测试（Mock SSDP）
- [ ] 编写 `tests/test_cast_dialog_integration.py`
- [ ] Mock `discover_renderers()` 返回测试设备
- [ ] 测试完整流程：扫描 → 显示 → 选择 → 返回
- [ ] 测试与 `CastSession` 的数据传递
- **工作量**：1 天，复杂度中等

#### Phase 5：手工验收与优化（预期 1-2 天）

**任务 5.1**：功能验收
- [ ] 真实电视环境测试设备发现
- [ ] 测试防火墙/网络异常场景
- [ ] 测试去重、刷新、选择完整流程（修订 2026-10-04：无缓存场景）
- [ ] 验证错误提示的清晰度
- **工作量**：1 天，复杂度中等

**任务 5.2**：性能与优化
- [ ] 确保 SSDP 扫描不阻塞主线程
- [ ] 优化列表显示性能（大设备数量）
- [ ] 验证内存泄漏（线程清理、信号连接）
- **工作量**：0.5 天，复杂度低

### 6.2 预期工作量估算

| 阶段 | 任务数 | 预期工作量 | 复杂度 |
|------|--------|----------|--------|
| Phase 1：基础组件 | 3 | 3 人·天 | 中等 |
| Phase 2：交互与刷新 | 3 | 2 人·天 | 中等 |
| Phase 3：进度与错误 | 3 | 2 人·天 | 中等 |
| Phase 4：单元与集成测试 | 3 | 3 人·天 | 中等 |
| Phase 5：验收与优化 | 2 | 1.5 人·天 | 低 |
| **总计** | **14** | **11.5 人·天** | **中等** |

### 6.3 关键路径与里程碑

```
[Phase 1 - Day 1-3]
  ├─ 基础 UI 框架 ✓
  ├─ SsdpScanWorker ✓
  └─ 设备列表显示 ✓
                     ↓
[Phase 2 - Day 3-5]
  ├─ 选择与确认 ✓
  ├─ 设备去重与刷新 ✓
  └─ 刷新逻辑 ✓
                     ↓
[Phase 3 - Day 5-7]
  ├─ 进度提示 ✓
  └─ 错误处理 ✓
                     ↓
[Phase 4 - Day 7-10]
  ├─ 单元测试 ✓
  └─ 集成测试 ✓
                     ↓
[Phase 5 - Day 10-11]
  ├─ 手工验收 ✓
  └─ 性能优化 ✓
                     ↓
[最终交付] ✓
```

**关键依赖**：
- Task 1（DLNA 模块）：Task 4 的基础，提供 `discover_renderers()` 和 `DlnaRenderer` 类
- Task 2（媒体代理局域网端点）：后续投屏流程需要
- Task 3（CastSession）：对话框的使用方

### 6.4 风险与缓解

| 风险 | 影响 | 缓解方案 |
|------|------|---------|
| SSDP 发现失败率高 | 用户体验差 | 提供多个错误提示和重试机制；支持刷新重试 |
| 防火墙频繁阻止 | 难以调试 | 明确提示防火墙配置步骤；可考虑后续检测模块 |
| 大网络设备数多 | 列表卡顿 | 限制显示 50 台以内；支持搜索/过滤（后续功能） |
| 线程清理不彻底 | 内存泄漏 | 严格测试线程生命周期；`closeEvent()` 中主动清理 |
| 跨平台兼容性 | Mac/Linux 异常 | 当前设计仅针对 Windows；后续跨平台需扩展 |

---

## 附录：API 参考

### A1. CastDeviceDialog 类接口

```python
class CastDeviceDialog(QDialog):
    """投屏设备选择对话框"""

    def __init__(self, current_episode_title: str, parent: QWidget | None = None):
        """
        初始化对话框

        Args:
            current_episode_title: 当前集标题（显示在对话框顶部）
            parent: 父窗口
        """
        ...

    def showEvent(self, event: QShowEvent) -> None:
        """对话框显示时自动启动扫描"""
        ...

    def closeEvent(self, event: QCloseEvent) -> None:
        """对话框关闭时清理资源"""
        ...

    def selected_device(self) -> DlnaRenderer:
        """
        返回用户选中的设备

        Returns:
            DlnaRenderer 对象，包含设备信息

        Raises:
            RuntimeError: 用户未选中任何设备
        """
        ...

    def _start_scan(self) -> None:
        """启动后台 SSDP 扫描"""
        ...

    def _on_device_discovered(self, renderer: DlnaRenderer) -> None:
        """处理设备发现信号"""
        ...

    def _on_scan_complete(self) -> None:
        """处理扫描完成信号"""
        ...

    def _on_scan_error(self, error_type: str, error_msg: str) -> None:
        """处理扫描错误信号"""
        ...

    def on_refresh_clicked(self) -> None:
        """处理刷新按钮点击"""
        ...

    def on_device_selected(self) -> None:
        """处理设备选择变化"""
        ...
```

### A2. SsdpScanWorker 类接口

```python
class SsdpScanWorker(QRunnable):
    """后台 SSDP 扫描任务"""

    device_discovered = pyqtSignal(DlnaRenderer)  # 发现新设备
    scan_complete = pyqtSignal()                  # 扫描完成或超时
    scan_error = pyqtSignal(str, str)             # 错误信号 (error_type, error_msg)

    SSDP_TIMEOUT = 2.0  # 扫描超时（秒）

    def run(self) -> None:
        """后台线程入口"""
        ...

    def cancel(self) -> None:
        """取消扫描"""
        ...
```

### A3. 集成示例

```python
# 在 video_view.py 中
def _on_cast_clicked(self):
    current_title = self._get_current_episode_title()

    dialog = CastDeviceDialog(current_title, parent=self)
    result = dialog.exec()

    if result == QDialog.Accepted:
        selected_renderer = dialog.selected_device()
        self._start_casting_workflow(selected_renderer)
```

---

**文档结束**

最后更新：2026-10-04（修订缓存策略：刷新清空缓存、按 UDN 去重回填、暂不做 QSettings 持久化）
