# Claw — 多源小说/漫画/视频聚合阅读器

基于 **PySide6 + 配置驱动爬虫内核** 的桌面应用：一个 GUI 同时挂载多个小说、漫画、视频源，规则全部外置（源 JSON + 广告规则），支持浏览、搜索、阅读、下载、书架、源管理。

## 功能总览

| 模块 | 说明 |
|---|---|
| 首页 | 源统计、最近搜索、吉祥物状态、快捷入口 |
| 发现 | 按源浏览分类/榜单，卡片网格 + 详情抽屉 |
| 搜索 | 多源并发搜索，按源筛选、封面解密回填 |
| 阅读器 | 小说（滚动/翻页）、漫画（懒加载/预加载/缩放）、视频（外部播放器） |
| 下载 | 按类型队列下载（并发/暂停/恢复/取消/重试），epub 合成 |
| 书架 | 收藏 + 续读记忆 + 本地已下载聚合 |
| 源管理 | 源启停、自检、权重、删除、可视化编辑器 |
| 设置 | 网络/UI/下载/书架/诊断/广告规则 |

全局快捷键：`Ctrl+1~8` 切换 Tab，`Ctrl+F` 聚焦搜索，`Ctrl+Enter` 执行搜索。

## 运行

```bash
# 依赖
pip install PySide6            # 必需
pip install requests lxml      # 可选，自动升级 HTTP/HTML 解析
pip install playwright         # 可选，JS 渲染型源（搜索/正文）需要
# 视频播放（外部播放器，二选一）
#   1) VLC 桌面版（推荐，自动探测常见安装路径）
#   2) 浏览器兜底

# 启动
python gui/app.py
```

打包：`python build.py`（PyInstaller，产物在 `dist/`）。

## 架构

```
┌─ gui/          PySide6 界面（pages 页面 + components 组件）
├─ framework/    配置驱动爬虫内核（纯标准库，可选依赖降级）
├─ sources/      源配置 JSON（36 个：小说/漫画/视频）
├─ tests/        pytest 单测
└─ docs/         架构、源配置 schema v2、UI 设计文档
```

依赖方向单向：`gui → framework`，`framework` 不依赖 GUI。所有爬虫规则（选择器、端点、解密、分页、广告）外置到 `sources/*.json`，不改代码即可增源。

## framework 内核（framework/）

| 模块 | 职责 |
|---|---|
| `config.py` | 源配置 v2 schema 解析 + 校验 |
| `http.py` | HTTP 请求（UA/Referer/代理池/反爬重试/间隔限速） |
| `parser.py` | HTML/CSS 选择器（lxml，缺失降级标准库） |
| `discovery.py` | 分类/列表/翻页（HTML 或 JSON API） |
| `search.py` | 多源搜索（并发分波、结果合并去重、interval 限速） |
| `content.py` | 详情/章节/正文/播放地址（解密、换源线路、JS 渲染） |
| `decrypter.py` | 外置解密（MacCMS base64+urldecode、AES-CBC 等） |
| `signers.py` | 接口签名（wbi 等，预留） |
| `adblock.py` | 广告过滤：URL 特征/域名黑名单/query 参数/m3u8 广告段/HTML 广告节点 |
| `media_proxy.py` | **本地流媒体代理**：目标 URL + 源 headers 打包成本地地址交给播放器（根治 VLC 无法设 UA），m3u8 分片/加密 key 自动重写 |
| `external_player.py` | 外部播放器：VLC 桌面版优先（带防盗链头走本地代理），浏览器兜底 |
| `downloader.py` / `download_queue.py` | 按类型下载 + 并发队列（暂停/恢复/重试/epub 合成） |
| `selfcheck.py` / `health.py` | 源结构自检 + 健康状态持久化 |
| `proxy_pool.py` / `cookie_manager.py` / `acw_solver.py` | 反爬支撑：代理池、cookie 管理、ACW 验证 |
| `playwright_helper.py` | JS 渲染页提取（图片/文本，资源自动回收） |

## 源配置（sources/*.json）

每个源一个 JSON，schema v2（见 `docs/source-schema-v2.md`）：

```jsonc
{
  "$schema_version": 2,
  "$id": "demo",          // 唯一标识
  "$type": "novel",       // novel / comic / video
  "$name": "演示站",
  "$enabled": true,
  "transports": { "base_url": "...", "headers": { "User-Agent": "...", "Referer": "..." } },
  "endpoints": {
    "discovery": { /* 分类 + 列表选择器 */ },
    "search":    { /* 搜索端点 + 结果选择器 */ },
    "detail":    { /* 详情字段 */ },
    "content":   { /* 章节/正文/播放地址提取 */ }
  },
  "decryption": { "targets": { "image": { "strategy": "aes_cbc", ... } } },
  "ad_block":   { "enabled": true, "block_domains": [...], "extra_css": [...] },
  "diagnostics": { "selfcheck": { "strategy": "soft", "selector": { ... } } }
}
```

要点：
- **全部规则外置**：选择器、端点、分页、解密、广告、自检都在 JSON 里，框架零硬编码。
- **解密**：图片/正文加密（AES-CBC 等）配置在 `decryption`；播放地址 MacCMS encrypt 等走 `decrypter`。
- **换源线路**（MacCMS 多线路站）：`source_switch` 配置线路解析，默认线路失效自动轮换其他线路。
- **JS 渲染**：需要浏览器执行的页面配置 `render: "playwright"`（搜索、正文、图片解密）。

## 视频播放链路

```
取流 (fetch_video_streams) ──► 本地代理 (media_proxy) ──► VLC / 浏览器
```

- 取流支持：m3u8(HLS，含 AES-128 加密)、mp4 直链、DASH 双流、yt-dlp 签名流。
- 默认线路 403/404 → 自动尝试其他播放源线路。
- 带防盗链头（Referer/UA/Cookie）的媒体走本地回环代理，VLC 只需播普通本地 URL，根治 VLC 命令行无法设置 User-Agent 的问题。
- 播放地址经 adblock 过滤（URL 特征 + m3u8 广告段剔除）。

## 测试

```bash
python -m pytest tests/ -q      # 79 个用例（adblock 规则 / 章节排序 / 搜索合并等）
```

GUI 离屏冒烟测试在 `gui/_smoke_*.py`（回归入口 `gui/_smoke_regression.py`）。

## 目录约定

- `sources/`：源配置；新增源 = 新建 JSON + 加载即用。
- `downloads/`：下载输出（gitignore）。
- `data/`：运行时数据（书架/历史/续读/健康，gitignore）。
