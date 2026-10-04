# Task 6：兼容性与全量回归验证计划

**文档版本**：1.0  
**创建时间**：2026-01-04  
**范围**：Task 1-5 完整功能的兼容性验证与性能基线建立

---

## 一、概述

Task 6 作为整个投屏功能的收口阶段，负责验证：
- 本地 VLC 播放的不变性
- 投屏端点生命周期的隔离性
- DLNA 协议的完整性
- 系统的并发与线程安全
- 异常场景的恢复能力
- 性能指标符合预期

**验证原则**：不引入新功能，聚焦于已有功能的全量覆盖与交叉验证。

---

## 二、验证域 1：本地 VLC 播放不变性

### 目标
确认未投屏时本地播放行为与投屏前完全一致，验证 Task 1-5 未对本地播放路径产生副作用。

### 验证项

#### 2.1 代理地址与访问链
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 未投屏时本地代理地址 | 保持为 `http://127.0.0.1:{local_port}/playlist.m3u8` | 启动 GUI，无投屏操作，检查日志中代理地址 |
| 代理端口固定性 | 端口号固定，不因投屏操作变化 | 记录多次启动的端口号，对比不变 |
| 本地 m3u8 可访问 | 直接 HTTP GET `/playlist.m3u8` 返回 200 | 单元测试 + 集成测试验证 |
| 分段文件 URI 生成 | 返回 `{base_url}/segment-{id}.ts` | 解析 m3u8，验证分段 URI 格式 |

#### 2.2 Token 管理
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 本地 token 生成 | 未投屏时正常生成，格式符合规范 | 启动播放，检查 token 是否存在于日志 |
| Token 过期策略 | 按原有策略执行，投屏操作不影响 | 长时间播放 > 1 小时，检查 token 刷新时序 |
| Token 作用域隔离 | 本地 token 仅对本地代理有效 | 尝试用本地 token 访问 LAN 端点，应拒绝 |

#### 2.3 缓存与预取
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 本地缓存目录 | 缓存路径不变，文件组织结构保持 | 检查 `~/.claw/cache/` 目录结构 |
| 分段缓存 | 播放后分段正确缓存 | 启动播放，检查缓存文件数量与大小 |
| 预取队列 | 预取队列独立管理，不因投屏重置 | 启动预取，记录队列状态；投屏再返回，队列保持 |
| 预取 URI 格式 | 预取 URI 格式与本地播放一致 | 检查预取日志中的 URI 格式 |

#### 2.4 播放控制流
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 播放/暂停 | 本地播放 play/pause 命令有效 | GUI 测试：点击播放/暂停按钮 |
| 进度条拖拽 | Seek 操作生效，无异常 | GUI 测试：拖动进度条到不同位置 |
| 音量控制 | 音量调节正常 | GUI 测试：调整音量滑块 |
| 停止播放 | Stop 后代理关闭，资源释放 | 启动播放 → Stop → 验证代理进程退出 |

#### 2.5 回归测试覆盖
| Task | 回归项 | 验证点数 |
|------|--------|--------|
| Task 1 | HLS 元数据解析 | 5 |
| Task 2 | 分段预取 | 4 |
| Task 3 | 本地代理 URI 路由 | 6 |
| Task 4 | CastSession 本地分支 | 3 |
| Task 5 | GUI 播放控制 | 4 |
| **小计** | | **22** |

---

## 三、验证域 2：投屏端点生命周期隔离

### 目标
确认投屏端点的启停不影响本地播放，token 和缓存的作用域严格隔离。

### 验证项

#### 3.1 LAN 端点启停隔离
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 启动投屏 | LAN endpoint 启动，获得临时 token | 调用 `start_lan_endpoint()` → 检查返回 token |
| 本地播放继续 | 投屏启动后本地播放不中断 | 播放中投屏，检查本地 m3u8 仍可访问 |
| 停止投屏 | `stop_lan_endpoint()` 后 LAN token 失效 | Stop 后用 LAN token 访问端点，应 401 |
| 本地 token 存活 | Stop 后本地 token 仍有效 | Stop 后访问本地代理 `/playlist.m3u8`，应 200 |

#### 3.2 资源访问隔离
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| m3u8 子资源隔离 | LAN 端点 m3u8 独立于本地 | 投屏时访问 LAN m3u8 vs 本地 m3u8，内容不同 |
| 分段文件隔离 | LAN 端点分段 URI 与本地不同 | 对比两端返回的分段 URI 格式 |
| Stop 后 m3u8 访问 | Stop 后本地 m3u8 仍可正常访问 | Stop → 访问本地 `/playlist.m3u8`，应 200 |
| Stop 后 LAN 分段不可访问 | LAN 分段 URI 在 Stop 后返回 404 | Stop → 用 LAN URI 访问分段，应 404 或 401 |

#### 3.3 预取状态隔离
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 预取队列独立 | 本地预取队列与 LAN 预取队列分离 | 启动预取 → 投屏 → 检查队列是否分离 |
| LAN start 不重置预取 | `start_lan_endpoint()` 不清空本地预取队列 | 投屏前预取进度 = 投屏后预取进度 |
| Stop 后预取继续 | Stop LAN 端点后本地预取继续 | Stop → 检查本地预取继续 |
| 缓存共享验证 | 本地与 LAN 可共享已缓存分段 | 本地预取分段 → LAN 访问同一分段，命中缓存 |

#### 3.4 Token 作用域测试
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 本地 token 跨投屏生命周期有效 | 投屏前后本地 token 保持有效 | Start → Stop → 用本地 token 访问 |
| LAN token 仅在投屏期间有效 | Start 前后 LAN token 无效 | Start 前和 Stop 后用 LAN token，应拒绝 |
| Token 过期独立 | 本地 token 过期不影响 LAN token 反之亦然 | 伪造过期本地 token，LAN 访问仍正常 |

---

## 四、验证域 3：DLNA 协议完整性

### 目标
确认 DLNA 协议的所有关键路径在真实或模拟电视环境下正确实现。

### 验证项

#### 4.1 SOAP Action 完整性
| SOAP Action | 功能 | 验证方法 | 优先级 |
|-----------|------|--------|--------|
| `GetProtocolInfo` | 返回设备支持的协议 | Mock SOAP 请求，验证响应格式 | P0 |
| `GetCurrentTransportInfo` | 获取传输状态 | 投屏中发送请求，验证状态转换 | P0 |
| `GetMediaInfo` | 获取媒体信息 | 投屏中发送请求，验证返回内容 | P0 |
| `SetAVTransportURI` | 设置播放 URI | 发送 URI，验证设备接受 | P0 |
| `Play` | 开始播放 | 发送 Play 命令，验证状态变化 | P0 |
| `Pause` | 暂停播放 | 发送 Pause 命令，验证状态 | P0 |
| `Stop` | 停止播放 | 发送 Stop 命令，验证清理 | P0 |
| `Seek` | 跳转 | 发送 Seek 命令，验证位置更新 | P1 |
| `GetPositionInfo` | 获取当前位置 | 周期性查询位置，验证进度 | P0 |
| `GetDeviceCapabilities` | 获取设备能力 | 查询支持的格式，验证列表完整 | P1 |

#### 4.2 Metadata 解析
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 标题提取 | 从 m3u8 EXTINF 提取标题 | 解析 m3u8，检查标题准确性 |
| 时长解析 | 正确解析 EXTINF 时长 | 对比 EXTINF duration 与解析值 |
| URI 编码 | URL 正确编码，特殊字符处理 | 传入含特殊字符的标题，验证编码 |
| XML 转义 | SOAP 请求中的 XML 转义 | 检查返回的 SOAP XML 是否正确转义 |
| 多语言支持 | 非 ASCII 字符正确处理 | 中文、日文标题投屏验证 |

#### 4.3 时间解析与同步
| 验证点 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 时长格式 | HH:MM:SS 格式正确解析 | 解析多种时长格式 |
| 位置更新周期 | 轮询周期稳定，延迟可控 | 记录轮询间隔，统计方差 |
| 时间同步漂移 | 客户端时间与服务器同步误差 < 1s | 对比客户端与服务器时间 |
| 毫秒级精度 | 如支持毫秒格式，精度达到 ms | 发送毫秒格式的 Seek，验证精度 |

#### 4.4 错误处理
| 错误场景 | 预期行为 | 验证方法 |
|--------|---------|---------|
| 设备不支持格式 | 返回 UPnP 错误码，客户端处理 | Mock 返回格式不支持错误 |
| 无效 URI | 返回 UPnP 错误码 | 发送无效 URI，验证错误处理 |
| 网络超时 | 重试逻辑启动，最终失败降级 | 模拟网络延迟，验证重试 |
| SOAP 解析失败 | 捕获异常，日志记录 | Mock 返回畸形 SOAP，验证处理 |
| 设备离线 | 检测离线，清理会话 | 停止 Mock 设备，验证离线检测 |

#### 4.5 真实与模拟环境验证
| 验证场景 | 硬件/工具 | 验证点 |
|---------|----------|-------|
| 真实电视 | LG/Samsung/Sony 电视 (可选) | 完整 SOAP 交互、实时渲染 |
| 高保真模拟器 | upnp-inspector + Mock UPnP 设备 | SOAP 格式、状态转换、错误码 |
| 协议检查器 | Wireshark DLNA 插件 | SOAP 报文格式、HTTP 头部 |

---

## 五、验证域 4：并发与线程安全

### 目标
确保多线程环境下无竞态条件、死锁或资源泄漏。

### 验证项

#### 5.1 CastSession 与 GUI Worker 并发
| 验证点 | 场景 | 预期行为 | 验证方法 |
|--------|------|---------|---------|
| 状态一致性 | GUI 更新进度 + 轮询线程读取 | 无脏读，状态原子更新 | ThreadSanitizer 或 Python 并发测试 |
| URI 更新 | 用户点击新集 + 轮询线程发 GetPositionInfo | 无 TOCTOU 问题 | Mock 场景，验证顺序 |
| Token 刷新 | 后台刷新 token + 代理访问 | Token 原子更新，无中间态 | 注入延迟，验证一致性 |
| 播放控制 | GUI 发送 Play/Pause + 轮询更新状态 | 控制命令与状态同步 | 顺序验证 |

#### 5.2 后台轮询与用户操作交错
| 验证点 | 场景 | 预期行为 | 验证方法 |
|--------|------|---------|---------|
| Seek 与轮询 | 用户 Seek + 轮询线程 GetPositionInfo | Seek 命令优先，轮询读取新位置 | Mock 网络延迟，验证顺序 |
| Stop 与轮询 | 用户 Stop + 轮询线程活跃 | Stop 优先，轮询线程退出 | 检查线程状态 |
| 预取与轮询 | 预取线程 + 轮询线程并发 | 分段缓存读写安全 | ThreadSanitizer 验证 |
| 离线检测与恢复 | 轮询检测离线 + 用户重连 | 无重复操作，状态一致 | 模拟网络波动 |

#### 5.3 LAN Endpoint 多会话共享
| 验证点 | 场景 | 预期行为 | 验证方法 |
|--------|------|---------|---------|
| 端口共享 | 多个设备同时投屏 | LAN 端点复用，不冲突 | 启动多个 CastSession，验证端口 |
| Token 隔离 | 多设备各自 token | Token 作用域严格隔离 | 用设备 A 的 token 访问设备 B 的资源，应拒绝 |
| 分段缓存线程安全 | 多会话并发缓存写入 | 缓存读写原子，无损坏 | ThreadSanitizer 验证 |
| 请求队列 | 多设备并发请求 LAN 端点 | 请求按序处理或并发安全 | 模拟高并发，检查响应正确性 |

#### 5.4 线程安全验证工具
| 工具 | 用途 | 检查点 |
|-----|------|-------|
| ThreadSanitizer (Python) | 检测竞态条件 | CastSession、PreFetcher、LocalProxy 类 |
| Lock/RLock 审计 | 验证锁持有时间 | 检查是否有锁持有过长 |
| Queue.Queue / threading.Event | 同步原语验证 | 确认同步点正确使用 |
| Deadlock 检测 | 死锁监测 | 线程间依赖关系验证 |

---

## 六、验证域 5：异常恢复

### 目标
验证系统在各种异常场景下的恢复能力，确保不影响本地播放。

### 验证项

#### 6.1 投屏失败恢复
| 故障场景 | 预期行为 | 恢复验证 | 验证方法 |
|---------|---------|--------|---------|
| 投屏初始化失败 | 弹出错误提示，本地播放继续 | 本地代理仍可访问 | 投屏失败 → 检查本地 m3u8 |
| SOAP 请求失败 | 重试 N 次，最终降级 | 本地播放不受影响 | 模拟 SOAP 失败，验证本地可用 |
| 设备不支持 URI | 显示不兼容提示，清理会话 | 本地播放继续 | Mock 设备返回不支持错误 |
| 投屏超时 | 超时后自动退出，释放资源 | 无资源泄漏 | 设置超时，检查资源释放 |

#### 6.2 设备断网恢复
| 故障场景 | 预期行为 | 恢复验证 | 验证方法 |
|---------|---------|--------|---------|
| 临时网络抖动 | 检测异常，自动重连 | 重连成功后继续播放 | 模拟网络中断 2-5s |
| 长期离线 | 检测离线，关闭投屏会话 | 用户可手动重新投屏 | 模拟断网 > 30s |
| 网络恢复 | 重新扫描设备，允许重连 | 可重新投屏到同一设备 | 恢复网络 → 重连投屏 |
| IP 变化 | 设备 IP 变化后仍可发现 | 重新建立连接 | 模拟 IP 变化 |

#### 6.3 电视不支持格式
| 格式类型 | 预期行为 | 恢复策略 | 验证方法 |
|---------|---------|--------|---------|
| 不支持 HLS | 返回 UPnP 错误，显示提示 | 本地播放继续 | Mock 设备不支持 HLS |
| 不支持编码 | 返回格式错误，显示提示 | 用户可尝试其他格式 | Mock 不支持编码 |
| 仅支持 DLNA 特定格式 | 尝试格式转换或提示用户 | 本地播放不受影响 | 配置仅支持 MPEG-TS |

#### 6.4 防火墙与网络阻断
| 阻断场景 | 预期行为 | 恢复验证 | 验证方法 |
|---------|---------|--------|---------|
| 入站规则阻止 | 投屏连接失败，显示网络错误 | 本地播放继续 | 关闭入站规则 |
| 出站规则阻止 | 无法连接设备，显示提示 | 本地播放继续 | 关闭出站规则 |
| 防火墙关闭后重试 | 允许用户手动重试 | 重试成功 | 启用防火墙 → 禁用 → 重试 |
| 代理路由阻断 | LAN 端点无法访问，显示 403/502 | 本地代理仍可用 | 模拟路由阻断 |

#### 6.5 异常恢复验证工具
| 工具 | 用途 |
|-----|------|
| pytest-timeout | 超时测试 |
| pytest-asyncio | 异步异常处理 |
| Mock + unittest.mock | 故障注入 |
| network-throttle | 网络模拟 |
| Chaos Monkey | 随机故障注入 |

---

## 七、验证域 6：性能基线

### 目标
建立关键性能指标的基线，确保系统满足实际使用需求。

### 验证项

#### 7.1 投屏启动延迟
| 指标 | 目标 | 测试场景 | 验证方法 |
|-----|------|---------|---------|
| 设备发现耗时 | < 3s | 同网段首次发现 | 记录 UPnP M-SEARCH 到设备响应的时间 |
| 连接建立耗时 | < 2s | SOAP 握手完成 | 记录第一个 SOAP 请求发送到收到响应的时间 |
| 首帧渲染耗时 | < 5s (总) | 从点击投屏到设备开始播放 | 记录投屏按钮点击到设备状态为 Playing 的时间 |
| 端点启动耗时 | < 1s | LAN endpoint HTTP 启动 | 记录 start_lan_endpoint() 调用到返回的时间 |

#### 7.2 轮询周期与 CPU 占用
| 指标 | 目标 | 测试场景 | 验证方法 |
|-----|------|---------|---------|
| 轮询周期 | 1-2s | 标准投屏操作 | 记录 GetPositionInfo 请求间隔 |
| CPU 占用 (轮询) | < 5% | 后台轮询无其他操作 | 投屏中记录 CPU 占用 |
| CPU 占用 (预取) | < 10% | 预取+轮询并发 | 投屏+预取中记录 CPU 占用 |
| 内存占用 (投屏会话) | < 50MB 增量 | 单个投屏会话 | Start → Stop，检查内存变化 |

#### 7.3 内存泄漏检测
| 验证点 | 场景 | 目标 | 验证方法 |
|--------|------|------|---------|
| 长时间投屏 | 投屏 1 小时，5 集连续播放 | 内存增长 < 20% | 记录投屏初期与 1h 后的内存占用 |
| 多会话开启/关闭 | 10 次 Start → Stop 循环 | 内存回到初始值±10% | 每次循环记录内存，绘制曲线 |
| 缓存积累 | 预取 1000+ 分段 | 缓存大小可控，未超过配置 | 检查缓存目录大小与配置上限 |
| Token 刷新周期 | 连续投屏 + Token 刷新 | 无未释放的 token | 检查内存中 token 对象数量 |

#### 7.4 带宽与网络效率
| 指标 | 目标 | 测试场景 | 验证方法 |
|-----|------|---------|---------|
| 预取带宽利用 | 填充缓存至配置大小 | 不超过 30MB/min | 测量预取 HTTP 流量 |
| 轮询开销 | GetPositionInfo 请求大小 | < 5KB/请求 | Wireshark 抓包统计 |
| 代理转发效率 | LAN 端点分段转发延迟 | < 100ms (P95) | 记录请求到响应的延迟 |

#### 7.5 性能测试工具与方法
| 工具 | 用途 |
|-----|------|
| `time.perf_counter()` | 精确计时 |
| `psutil` | CPU、内存监控 |
| `tracemalloc` | 内存分析 |
| `cProfile` | CPU profiling |
| `Wireshark` | 网络流量分析 |
| `Apache JMeter` | 并发压力测试 |
| `locust` | 分布式负载测试 |

---

## 八、验证域 7：测试工具与流程

### 8.1 单元测试矩阵

所有 Task 1-5 的单元测试应保留并执行。

#### 覆盖范围

| Task | 模块 | 单元测试文件 | 测试数量 | 状态 |
|------|------|-----------|--------|------|
| Task 1 | HLS Parser | `tests/unit/test_hls_parser.py` | 15+ | 应存在 |
| Task 2 | PreFetcher | `tests/unit/test_prefetcher.py` | 12+ | 应存在 |
| Task 3 | LocalProxy | `tests/unit/test_local_proxy.py` | 18+ | 应存在 |
| Task 4 | CastSession | `tests/unit/test_cast_session.py` | 20+ | 应存在 |
| Task 5 | GUI Logic | `tests/unit/test_gui_logic.py` | 10+ | 应存在 |
| **总计** | | | **75+** | |

#### 执行命令
```bash
pytest tests/unit/ -v --cov=claw --cov-report=html
```

#### 通过标准
- 所有单元测试 100% 通过
- 代码覆盖率 >= 80%
- 无新增失败

---

### 8.2 集成测试架构

集成测试验证 Task 1-5 各模块之间的交互。

#### 测试层次

```
集成测试 (Integration Tests)
├── LAN 代理层
│   ├── LocalProxy + PreFetcher（缓存命中）
│   ├── LocalProxy + HLS Parser（URI 生成）
│   └── LocalProxy + Token Manager（鉴权）
├── DLNA 层
│   ├── CastSession + SOAP Mock（命令下发）
│   ├── CastSession + 轮询器（状态同步）
│   └── CastSession + 错误处理（故障恢复）
├── UI 层
│   ├── GUI Worker + LocalProxy（进度更新）
│   ├── GUI Worker + CastSession（投屏控制）
│   └── GUI Worker + PreFetcher（预取 UI）
└── 端到端
    ├── 本地播放流程（GUI → LocalProxy → 缓存 → 分段）
    ├── 投屏流程（GUI → CastSession → DLNA → 设备）
    └── 故障恢复（异常注入 → 恢复验证）
```

#### 关键集成场景

| 场景 ID | 名称 | 涉及模块 | 验证点 |
|--------|------|--------|--------|
| I-001 | 本地播放 + 预取 | LocalProxy + PreFetcher | 缓存命中率 > 80% |
| I-002 | 投屏 + 状态轮询 | CastSession + Poller | 位置同步误差 < 1s |
| I-003 | 投屏启动/停止 | CastSession + LocalProxy | 本地播放不中断 |
| I-004 | GUI 控制流 | GUI Worker + CastSession | 命令执行延迟 < 500ms |
| I-005 | 投屏失败恢复 | CastSession + 错误处理 | 本地播放继续，可重试 |
| I-006 | 多设备投屏 | LAN Endpoint + 多 CastSession | Token 隔离，端口复用 |

#### 集成测试文件组织

```
tests/integration/
├── test_local_playback.py        # I-001, I-004
├── test_casting_flow.py          # I-002, I-003, I-005
├── test_multi_session.py         # I-006
├── test_error_recovery.py        # 故障恢复
└── conftest.py                   # Mock 设备、代理
```

---

### 8.3 端到端测试（E2E）

#### 8.3.1 真实电视测试（可选）

仅当有可用的真实 DLNA 设备时执行。

| 设备类型 | 测试点 | 预期结果 |
|---------|--------|---------|
| LG WebOS TV | SetAVTransportURI + Play | 电视播放 HLS 流 |
| Samsung SmartTV | 控制流（Play/Pause/Seek） | 远程控制成功 |
| Sony Bravia | 多集顺序播放 | 无 URI 重复、正确过渡 |

#### 8.3.2 高保真模拟环境

使用 UPnP 模拟器和 Mock DLNA 设备。

| 工具 | 用途 |
|-----|------|
| `upnp-inspector` | DLNA 设备模拟 |
| `gupnp` | UPnP 库模拟设备 |
| `mock-server` (Python) | SOAP 服务模拟 |

#### 8.3.3 真实源验证

使用真实视频源进行端到端验证。

| 源 | URL 格式 | 验证点 |
|-----|---------|--------|
| dm84 | `https://example.com/hls/playlist.m3u8` | 元数据解析、分段下载 |
| omofuna | `https://example.com/live/stream.m3u8` | 实时流、轮询同步 |

#### E2E 测试脚本框架

```python
# tests/e2e/test_casting_e2e.py
class TestCastingE2E:
    def test_local_playback_full_flow(self):
        # 启动 GUI → 选择视频 → 播放 → 验证 UI/缓存/分段
        pass
    
    def test_casting_to_mock_device(self):
        # 启动 GUI → 投屏 → 验证 SOAP/状态/位置同步
        pass
    
    def test_casting_failure_recovery(self):
        # 投屏失败 → 本地播放继续 → 重试成功
        pass
    
    def test_multi_episode_casting(self):
        # 投屏 5 集 → 验证过渡、无中断
        pass
```

---

### 8.4 测试执行流程

#### 8.4.1 日常测试（开发阶段）

```bash
# 快速单元测试
pytest tests/unit/ -x

# 选择性集成测试
pytest tests/integration/test_local_playback.py -v

# 代码覆盖率
pytest tests/unit/ --cov=claw --cov-report=term-missing
```

#### 8.4.2 完整回归测试（发布前）

```bash
# 清理环境
rm -rf ~/.claw/cache/
pkill -f claw

# 单元测试
pytest tests/unit/ -v --cov=claw

# 集成测试
pytest tests/integration/ -v

# E2E 测试（需要 Mock 设备）
pytest tests/e2e/ -v -m "not real_device"

# 性能测试
pytest tests/performance/ -v --benchmark-only

# 线程安全测试
pytest tests/concurrent/ -v --tb=short
```

#### 8.4.3 CI/CD 集成

```yaml
# .github/workflows/regression-test.yml
name: Full Regression Test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Unit Tests
        run: pytest tests/unit/ -v --cov=claw
      - name: Integration Tests
        run: pytest tests/integration/ -v
      - name: Performance Tests
        run: pytest tests/performance/ -v
      - name: Upload Coverage
        uses: codecov/codecov-action@v3
```

---

## 九、验证域 7.5：测试环境与工具配置

### 9.1 Mock 与模拟工具清单

| 工具 | 用途 | 配置 |
|-----|------|------|
| `unittest.mock` | Python 对象 Mock | 模拟 SOAP 调用、HTTP 请求 |
| `responses` 库 | HTTP Mock | 模拟投屏端点、DLNA 设备 |
| `pytest-mock` | pytest 集成 Mock | Fixture 支持 |
| `pytest-timeout` | 超时控制 | 防止测试挂起 |
| `pytest-xdist` | 并行执行 | 加速测试 |
| `faker` | 随机数据生成 | 生成随机 URI、token |

### 9.2 测试数据与配置

| 配置项 | 值 | 用途 |
|-------|-----|------|
| 本地端口范围 | 8000-9000 | 避免与系统端口冲突 |
| 轮询周期 | 1s | 测试场景 |
| 预取缓冲 | 50MB | 测试内存占用 |
| Token 有效期 | 1h | 测试刷新 |
| SOAP 超时 | 5s | 测试重试 |
| 离线检测阈值 | 30s | 测试离线检测 |

### 9.3 测试环境隔离

所有测试应在独立环境中执行，避免相互影响。

```python
# tests/conftest.py 全局配置
import pytest
import tempfile
import os

@pytest.fixture(autouse=True)
def isolated_env():
    """隔离测试环境"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 重定向缓存目录
        os.environ['CLAW_CACHE_DIR'] = tmpdir
        # 使用临时日志
        os.environ['CLAW_LOG_DIR'] = tmpdir
        yield
```

---

## 十、验收标准

### 10.1 功能验收

| 检查点 | 标准 | 验证方法 |
|--------|------|---------|
| 单元测试 | 100% 通过，0 新增失败 | `pytest tests/unit/` |
| 集成测试 | 100% 通过，0 新增失败 | `pytest tests/integration/` |
| E2E 测试 | ≥ 80% 通过（高保真模拟） | `pytest tests/e2e/` |
| 代码覆盖率 | ≥ 80% | `--cov` 报告 |

### 10.2 性能验收

| 指标 | 目标 | 验证方法 |
|-----|------|---------|
| 投屏启动延迟 | < 5s | 性能测试 |
| CPU 占用 (轮询) | < 5% | 监控工具 |
| 内存泄漏 | 无明显增长 | 1h 长时间测试 |
| 缓存命中率 | > 80% | 日志统计 |

### 10.3 兼容性验收

| 项目 | 标准 | 验证方法 |
|-----|------|---------|
| 本地播放不变性 | 投屏前后行为一致 | 对比日志、缓存 |
| 投屏端点隔离 | Token、资源、状态完全隔离 | 集成测试覆盖 |
| DLNA 协议 | 所有 SOAP action 正常 | Mock 设备验证 |
| 异常恢复 | 5 类故障场景均可恢复 | 故障注入测试 |

### 10.4 发布前检查清单

- [ ] 所有单元测试通过
- [ ] 所有集成测试通过
- [ ] 代码覆盖率 >= 80%
- [ ] 性能基线建立并符合预期
- [ ] 无内存泄漏（1h 长时间测试）
- [ ] DLNA 协议在 Mock 环境下完整
- [ ] 线程安全验证通过
- [ ] 异常恢复验证通过
- [ ] 代码审查完成
- [ ] 文档更新完毕

---

## 十一、测试执行时间线

| 阶段 | 工作项 | 预计耗时 |
|------|--------|---------|
| 准备 | 构建 Mock 环境、工具配置 | 2-3 天 |
| 单元测试 | 执行 Task 1-5 单元测试 | 1 天 |
| 集成测试 | 执行 6 个集成场景 | 2 天 |
| 性能测试 | 基线建立、瓶颈分析 | 2 天 |
| E2E 测试 | 高保真模拟 + 真实源 | 2 天 |
| 故障恢复 | 5 类故障场景验证 | 2 天 |
| 报告与修复 | 缺陷修复、性能优化 | 3-5 天 |
| **总计** | | **14-17 天** |

---

## 十二、文件与输出物

### 12.1 测试代码产出

```
tests/
├── unit/
│   ├── test_hls_parser.py (Task 1, 已有)
│   ├── test_prefetcher.py (Task 2, 已有)
│   ├── test_local_proxy.py (Task 3, 已有)
│   ├── test_cast_session.py (Task 4, 已有)
│   └── test_gui_logic.py (Task 5, 已有)
├── integration/
│   ├── test_local_playback.py (新)
│   ├── test_casting_flow.py (新)
│   ├── test_multi_session.py (新)
│   ├── test_error_recovery.py (新)
│   └── conftest.py (新)
├── e2e/
│   ├── test_casting_e2e.py (新)
│   ├── test_performance_e2e.py (新)
│   └── fixtures.py (新)
├── performance/
│   ├── test_latency.py (新)
│   ├── test_memory.py (新)
│   └── test_throughput.py (新)
└── conftest.py (全局)
```

### 12.2 测试报告产出

| 报告 | 格式 | 输出路径 |
|-----|------|---------|
| 单元测试覆盖率 | HTML | `htmlcov/index.html` |
| 集成测试结果 | JUnit XML | `test-results/integration.xml` |
| E2E 测试日志 | 文本 | `logs/e2e_test.log` |
| 性能基线 | CSV + 图表 | `reports/performance_baseline.csv` |
| 最终回归报告 | Markdown | `docs/regression-report.md` |

### 12.3 文档更新

| 文档 | 更新内容 |
|-----|---------|
| `docs/testing.md` | 测试框架、工具、运行方法 |
| `docs/performance.md` | 性能指标、基线、优化建议 |
| `docs/troubleshooting.md` | 常见问题、调试方法 |
| `README.md` | 测试运行说明 |

---

## 十三、风险与缓解

### 13.1 已知风险

| 风险 | 影响 | 缓解措施 |
|-----|------|---------|
| 真实电视不可得 | 无法验证实际 DLNA 兼容性 | 使用高保真 UPnP 模拟器 |
| 网络环境复杂 | 测试结果不稳定 | 使用 Mock 网络、控制环境 |
| 线程竞态难重现 | 间歇性失败 | ThreadSanitizer + 长时间运行 |
| 性能指标波动 | 难以建立稳定基线 | 多次运行取平均值 |

### 13.2 缓解措施

1. **Mock 优先**：使用 Mock 与模拟器，而非依赖真实设备
2. **长时间运行**：压力测试运行 > 1 小时，捕获偶现问题
3. **环境隔离**：每个测试使用独立的临时文件系统
4. **重复执行**：关键测试重复 10 次，统计稳定性
5. **日志详尽**：记录所有关键操作的时间戳与状态

---

## 十四、相关文档与参考

- Task 1 设计文档：`docs/superpowers/plans/task-1-hls-parsing.md`
- Task 2 设计文档：`docs/superpowers/plans/task-2-prefetching.md`
- Task 3 设计文档：`docs/superpowers/plans/task-3-local-proxy.md`
- Task 4 设计文档：`docs/superpowers/plans/task-4-cast-session.md`
- Task 5 设计文档：`docs/superpowers/plans/task-5-gui-integration.md`
- DLNA 规范：UPnP Device Architecture 1.0
- HLS 规范：RFC 8216

---

## 十五、签审

| 角色 | 签审 | 日期 |
|-----|------|------|
| 编写者 | OpenCode | 2026-01-04 |
| 审核者 | (待指定) | |
| 批准者 | (待指定) | |

---

**文档结束**
