# 视频 m3u8 广告过滤加强设计（m3u8-ad-filter-strengthen）

日期：2026-09-17
状态：用户已批准方案 A（通用启发式加强 + 视频流默认启用）

## 1. 背景与目标

用户反馈：广告过滤功能不够强，主要在**视频流的片头（开头的广告）与中插（中间的广告）**；要求在**阅读（播放）与下载都生效**。

现状诊断：

- `framework/adblock.py` 已有 `detect_m3u8_ads`：R1 URL 广告特征、R2 重复段 URL、R3 DISCONTINUITY 分隔的孤立短块（<3s）。
- **卡点 1（默认关闭）**：`AdblockEngine.configure()` 在源未配置 `ad_block` 时置 `enabled=False` → 只有 4 个源（18j/avgood/bilibili/xuandm）配了 `ad_block`，其余 40+ 源视频流完全不过滤。
- **卡点 2（播放路径）**：`media_proxy._filter_ad_segments` 仅在 `ad_block` 非空时过滤；且 `external_player.open_with_player` 只在有防盗链头时才走本地代理——无防盗链头的源播放 HLS 时既不进代理也不过滤。
- **卡点 3（识别盲区）**：顶不上片头预滚（列表首块被「不判」）、中插非短块（5~15s、URL 不重复、非 3s 内）广告。

## 2. 已确认决策

- 采用方案 A：识别规则增强（R4/R5/R6）+ 视频流默认启用内置规则。
- 不定位到单站：全部用通用启发式 + HLS 协议级标签，不做站点特征。
- 小说/漫画路径本次不动（`filter_text` / `_filter_ad_images` 保持原行为）。
- HLS 播放一律走本地代理以过滤广告（VLC 缓冲按代理预案已 +8000ms，可接受）。

## 3. 广告识别规则增强（adblock.py `detect_m3u8_ads`）

保留 R1/R2/R3 不动（有测试锁定），新增三条：

### R4 协议级广告标签（零误伤）

识别服务器自带广告声明的 HLS 标签，区间内段判广告：

- `#EXT-X-CUE-OUT` / `#EXT-X-CUE-OUT:...`、`#EXT-X-CUE-OUT-CONT`：广告区间开始（计数 +1）
- `#EXT-X-CUE-IN`：广告区间结束（计数 -1，下限 0）
- `#EXT-X-SCTE35-OUT` / `#EXT-X-SCTE35-IN`：同 CUE-OUT/IN 语义
- 遗留 `#EXT-X-SCTE35`：`CUE="...am_splice_type=0x2|0x0e..."`（placement/end）分别 +1/-1
- `#EXT-X-DATERANGE` 含 `SCTE35` / `X-AD` / `X-ASSET` 属性：视为广告区间开始（+1）

区间实现：解析走行时维护 `cue_depth` 计数，`cue_depth > 0` 时的段记入 `cue_ad_segs`。孤 CUE-OUT 无配对 → 持续到列表尾（服务器自声明，可接受）。

### R5 片头/片尾预滚（双信号）

- 作用于列表**首位块 / 末位块**（DISCONTINUITY 分隔，依据现有第二遍块划分）。
- 判广告条件（全部满足）：
  - 整体时长 `total_dur >= 60s`（防短视频误伤）
  - 块时长 `block_dur < 0.25 * total_dur`
  - 且 `block_dur <= 30s` **或** 块内所有段时长完全一致（uniform 签名，广告素材典型特征）

### R6 中插离群块（双信号）

- 作用于**内部块**（前后都有 DISCONTINUITY）。
- 判广告条件（全部满足）：
  - `block_dur < 各块时长中位数 ÷ 3`
  - 且块内时段长完全一致 **或** `block_dur < 10s`

### 判定管线

`detect_m3u8_ads` 第三遍重写前统一并入 `ad_set`；重写逻辑复用现有行回退机制（DISCONTINUITY / KEY:METHOD=NONE 清理，保留加密 KEY/MAP），不新写。异常静默：任何新规则解析失败回到现有 try/except，不过滤不阻断播放/下载。

## 4. 生效范围（阅读 + 下载都生效）

### 4.1 引擎默认策略

- `AdblockEngine.configure(source, default_on=False)`：源无 `ad_block` 时 `_enabled = default_on`；有 `ad_block` 仍尊重显式 `enabled`（缺省 true，显式 `false` 关闭）。
- `adblock_for(source, default_on=False)` 透传该参数。

### 4.2 下载三条链路传 `default_on=True`

- `downloader._filter_m3u8_for_download`（ffmpeg 合并路径）
- `downloader._download_hls`（自研逐段下载路径）
- `download_queue._spawn_ad_precheck`（加任务预检）

### 4.3 播放链路

- `media_proxy._filter_ad_segments`：去除「`ad_block` 为空就不过滤」守卫（`if ad_block:` → `if ad_block is not None:`），无配置时用内置规则构建引擎；显式 `enabled: false` 仍关闭。
- `external_player.open_with_player`：URL 为 HLS（`.m3u8`）且 VLC 可用时**一律走本地代理**（原来仅在有防盗链头时走代理）；浏览器兜底路径不过滤（降级可接受）。

## 5. 测试计划（离线 mock，无真实网络）

新增（扩展 `tests/test_adblock_html.py`）：

1. R4：CUE-OUT/CUE-IN 区段剔除；SCTE35-OUT/IN、DATERANGE（带 SCTE35 属性）区段剔除；孤 CUE-OUT 持续到列表尾。
2. R5：片头预滚（header → 3×5s 均匀广告 → DISCONTINUITY → 10×10s 正片）被剔；对照：首位块 90s 不剔；整体 <60s 短视频首位短块不剔。
3. R6：中插 4s 均匀块（各块中位数 20s）被剔；对照：12s 非均匀内部块保留。
4. 默认开启：无 `ad_block` 源 `adblock_for(source, default_on=True)` 能过滤；显式 `enabled: false` 仍关闭；现有 4 个配置源行为不变（回归）。
5. 既有 `test_adblock_html.py` 全部保持通过（回归）。

验证命令：

```powershell
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_adblock_html.py -q
```

定向通过后跑全量 `python -m pytest tests/ -q`，无 lint 配置（无 flake8/ruff/pyproject）。

## 6. 风险与兜底

- R5/R6 双信号 + 阈值保守，误删正片风险低；仍有疑虑用 R4 协议标签兜底。
- HLS 播放多一跳本地回环代理：VLC 缓冲已按代理预案 +8000ms，开销可接受。
- 异常时静默回原文：不过滤不阻断播放/下载，与现有 filter_m3u8 一致。
- 浏览器兜底播放不过滤：可接受的降级（VLC 是主路径）。