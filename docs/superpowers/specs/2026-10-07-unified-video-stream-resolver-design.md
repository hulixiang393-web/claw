# 统一视频取流与播放器兜底设计

## 背景

动漫巴士、Omofun、提线影院、饭搭子和 XDM 的播放流获取分别依赖 Playwright、source_switch、JS 正则或 SSR 数据。当前失败路径串行叠加请求超时和重试，导致取流极慢；某个选择器失败时也缺少统一候选解析。YouTube 的 yt-dlp 已能获取真实直链，但通用播放器策略因源配置中的 User-Agent 强制进入本地代理，代理访问 googlevideo 返回 403，VLC 最终无法打开本地 `/s/` MRL。

## 目标

- 统一管理视频流候选解析、超时、线路兜底和缓存。
- 单源取流有明确总时间预算，失败快速返回，不让多线路串行无限等待。
- 复用现有纯 HTTP、source_switch、Playwright、yt-dlp 解析能力。
- 成功流短期缓存，重复播放不重新解析。
- YouTube 等已拿到可直连签名 URL 的源不强制进入本地代理。
- 不绕过验证码、WAF、登录或防盗链机制。

## 统一取流流程

新增统一 resolver 层，输入 `source`、`episode_url`、`quality` 和播放用途，输出有序候选列表或首个可用流，并返回诊断信息。

候选顺序：

1. 内存流缓存，按 source、episode URL、quality、播放模式区分。
2. 直链媒体 URL 识别，直接返回 m3u8/mp4/mpd 等地址。
3. 源配置的纯 HTTP 解析，包括 `http_chain`、`source_switch`、JS 正则、DOM selector、JSON/API。
4. 其他已配置播放线路，去重后按源顺序尝试。
5. 仅当配置声明 `render: playwright` 或纯 HTTP候选全部失败时使用 Playwright。
6. YouTube 使用 yt-dlp，成功的签名 URL 直接作为候选，不经过本地代理包装。

每个源通过 `stream_budget` 配置总预算、单候选超时和最大候选数；未配置时使用保守默认值。候选请求共享取消/截止时间，超过总预算立即终止后续候选。Playwright 仍可有源级快速轮询与慢播放器兜底，但不能突破总预算。

## source_switch 兜底

保留现有 `source_switch` 配置格式，内部改为：

- 先解析详情页中所有线路和分集候选；
- 对相同播放地址去重；
- 线路候选按照配置顺序尝试；
- 单线路失败只记录诊断，不阻塞到全局默认重试上限；
- 解析出 URL 后交给统一可用性检查和播放入口；
- 所有候选失败时返回聚合错误，包含源名、候选数量和失败阶段。

不在后台无限重试，也不将失败线路缓存为成功结果。

## 源级适配重点

- 动漫巴士：保留快速 Playwright iframe/媒体请求解析，使用 500ms 首轮轮询和源级关闭慢回退；失败时返回明确错误。
- Omofun：保留 `player_aaaa` 解码和多线路，增加 JS 变量、iframe、video/source 候选的统一收集，避免只依赖单个正则。
- 提线影院：保留 `var now` 解析，增加相对 URL 绝对化、iframe/video/source 兜底和线路候选去重。
- 饭搭子：保留 `player_aaaa`/maccms 解密，统一尝试 HTML、播放器配置和线路候选，限制总等待。
- XDM：保留 SSR/API 解析，支持未转义 JSON、转义 JSON 和 video/source 候选；m3u8 直接候选化。
- YouTube：yt-dlp 失败后按现有格式回退；成功的 googlevideo URL 直接播放，不因为通用 UA 进入本地代理。

## VLC 与本地代理

修改播放器代理判定：

- `Referer`、Cookie 或源明确要求代理时继续使用本地 MediaProxy。
- 只有通用 User-Agent 时不强制代理；VLC 直接打开真实媒体 URL。
- YouTube `googlevideo.com`、已签名 CDN URL 默认直连。
- 保留 HLS 广告过滤和必须代理的防盗链源行为。
- 播放器收到本地代理 URL 时保留现有 lease、Range 和错误处理。

## 缓存与并发

- 取流成功缓存短 TTL，按源、集、画质和播放模式隔离。
- 不缓存失败结果和过期签名 URL。
- 同一集并发取流合并为一个任务，后续请求等待同一 Future，避免重复启动 yt-dlp/Playwright。
- 换源、换作品、质量变化时使相关缓存失效。

## 可观测性与错误处理

记录阶段耗时：缓存命中、直链、HTTP、线路、Playwright、yt-dlp、代理探测；不记录完整签名 URL、Cookie 或敏感参数。最终错误显示简短原因，详细诊断写日志。

## 测试

- 每个目标源使用脱敏 fixture 覆盖至少一个成功候选和一个失败后兜底候选。
- 统一 resolver 测试缓存命中、候选去重、总预算、超时截止、并发合并和失败聚合。
- YouTube 测试带 User-Agent 的 googlevideo URL 不生成本地代理 URL；需要 Referer 的源仍生成代理 URL。
- VLC 测试直链 MRL 与本地代理 MRL 分类保持正确。
- 运行视频源单测、播放器/MediaProxy 单测、compileall、GUI video smoke 和 diff check。
