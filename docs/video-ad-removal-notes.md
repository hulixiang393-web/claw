# 视频流内广告去除（任务队列预检）改动记录（2026-08-09）

## 需求
用户反馈：要去除**视频播放中间插入的广告（mid-roll/流内广告）**，并在**添加任务队列时**进行处理——不是去除页面/正文广告。

## 实现方案
三层联动：

### 1. adblock.py —— `detect_m3u8_ads()`（新增检测接口）
- 复用 `filter_m3u8` 的判定逻辑，拆成 `detect_m3u8_ads(text, base_url) -> (ad_seg_indices, cleaned_text)`
- 返回：被判定为广告的**段序号列表** + 剔除后的播放列表
- 判定启发式（与 filter_m3u8 一致）：
  1. 段 URL 命中广告特征（域名黑名单/路径/query/源级规则）
  2. 紧跟 `#EXT-X-DISCONTINUITY` 且时长 < 3s 的短段
- `filter_m3u8` 改为内部调用 `detect_m3u8_ads`，行为不变（无广告返回原文，调用方据此判断是否过滤）

### 2. download_queue.py —— `add_task` 预检（核心）
- `DownloadTask` 新增字段：`has_ads: bool`、`ad_segments: dict`（{章节索引: 广告段序号列表}）
- `add_task` 对**视频任务**（total>0）启动后台线程 `_spawn_ad_precheck`：
  - 逐集调 `fetch_video_streams` 取流 → 是 m3u8 则下载播放列表 → `detect_m3u8_ads`
  - 检测到广告 → 标记 `task.has_ads=True` + `task.ad_segments[章节索引]=段列表`，发进度事件（payload 带 `has_ads: True`，UI 可展示）
  - **失败静默**：网络/超时/限流不阻塞入队，下载时 filter_m3u8 兜底
  - 非 HLS（mp4/dash）自动跳过（无流内广告段概念）

### 3. downloader.py —— 全下载路径接入
- **`_download_hls`（自研 HLS 逐段下载，最常用路径）**：
  - 新增 `source` 参数；解析段后调 `detect_m3u8_ads` 得 `ad_segs`
  - 段循环中 `idx in ad_set` 的段**跳过下载**（不写文件、不进本地 m3u8）
  - 本地 m3u8 序号与文件名均按原始 idx 生成，跳过后无缝衔接
- **`_merge_video_ffmpeg`（ffmpeg 合并路径）**：
  - **移除 Referer 跳过限制**——带 Referer 的源也尝试本地过滤（ffmpeg `-headers` 会传播给本地 m3u8 引用的远程段请求，`-protocol_whitelist` 已放行 http/https）
  - 过滤失败静默回退原始 URL，保证能下

### 未改动（有意为之）
- **yt-dlp 引擎源**（YouTube 等）：本身无 m3u8 流内广告问题，且签名/合并链路复杂，不强行接入
- **yt-dlp 泛 HLS 兜底**：仅是 ffmpeg 失败后的备用路径，主路径已覆盖广告过滤

## 效果
- **加任务时**：视频任务后台预检，任务卡片可显示「含广告，下载自动剔除」
- **下载时**：自研 HLS + ffmpeg 两条主路径都会剔除流内广告段（跳过下载/合并）
- **零阻塞**：预检失败/超时完全静默，不影响入队与下载

## 验证
`python _verify_ads.py` 全部通过：
1. detect_m3u8_ads：广告段索引正确（[1]），清理后保留正常段、DISCONTINUITY 清理合法
2. 无广告 m3u8：返回 [] + 原文不变
3. filter_m3u8 与 detect_m3u8_ads 一致性
4. DISCONTINUITY 短段启发式：非广告域名短段（1.5s）也正确判广告
5. 段循环跳过逻辑：广告段不进下载列表
6. DownloadTask 新字段：has_ads/ad_segments 默认值正确
- py_compile framework/adblock.py / download_queue.py / downloader.py 通过

## 文件改动
- framework/adblock.py：新增 `detect_m3u8_ads`（filter_m3u8 重构为调用它）
- framework/download_queue.py：`DownloadTask.has_ads/ad_segments` + `_spawn_ad_precheck`
- framework/downloader.py：`_download_hls` 广告段跳过 + `_merge_video_ffmpeg` 移除 Referer 限制
