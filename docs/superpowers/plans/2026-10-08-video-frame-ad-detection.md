# 视频分片画面广告检测实施计划

**Goal:** 使用免费 FFmpeg/OpenCV 对 HLS 分片进行异步双帧检测，只有高置信度广告才在后续清单重写时删除。

**Constraints:** 默认关闭；不阻塞首播；无法确认时保留；不修改用户配置；不提交；不添加代码注释；不记录签名 URL/Cookie。

## Task 1：特征检测模块

- 新建 `framework/frame_ad_detector.py`。
- 提供 `FrameAdDetector.detect(segment_bytes, context=None) -> DetectionResult`。
- 使用 FFmpeg/OpenCV 可选依赖；缺失时返回 unknown。
- 抽取开头和中间两帧，缩放 320×180；计算帧率、分辨率、亮度、饱和度、黑/纯色比例、感知哈希和重复度。
- 单一异常特征不得标记广告；结果必须包含 confidence 和 reasons。
- 添加 `tests/test_frame_ad_detector.py`，覆盖正常、疑似、解码失败、依赖缺失。

## Task 2：MediaProxy 异步接入

- 在 `framework/media_proxy.py` 增加可选 detector、短 TTL 缓存和限并发任务。
- 清单首次返回不等待检测；后台检测分片并缓存脱敏分片指纹。
- 下一次同一清单重写时，只有高置信度结果才跳过分片。
- 不改变协议标签、KEY、MAP、BYTERANGE 和音视频同步；检测失败保留分片。
- 支持 source/ad_block 的 detector 配置，不强制对所有源启用。
- 添加 MediaProxy 回归测试，验证不阻塞、TTL、取消和高置信度删除。

## Task 3：配置与集成

- 增加通用 `media.frame_ad_detection` 配置读取，默认 enabled=false、sample_frames=2、320×180、threshold=0.9、concurrency=1、TTL=900。
- 播放/投屏共用同一配置和缓存，下载路径不启用画面检测。
- 添加配置及视频播放集成测试。

## Task 4：验证

- 运行相关广告、MediaProxy、视频播放、投屏回归。
- 运行 compileall、git diff --check。
- 确认 OpenCV/FFmpeg 缺失时普通播放不受影响。
