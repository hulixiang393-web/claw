# adblock 广告过滤完善记录（2026-08-09）

## 背景
用户反馈：广告过滤中间件不完善，部分源的广告无法过滤修复。

## 改动清单

### framework/adblock.py（核心增强）
1. **域名黑名单大幅扩充**（~10 → ~70 个）
   - Google 系：doubleclick/googlesyndication/googletagservices/googleadservices/google-analytics/googletagmanager/adservice.google/admob/2mdn
   - 欧美 DSP/原生广告：adnxs/taboola/outbrain/smartadserver/criteo/pubmatic/rubiconproject/openx/casalemedia/moatads/adform/adroll/adcash/adsterra/popads/exoclick/mgid/revcontent/sharethrough/media.net/teads/spotxchange/brightroll
   - 移动广告 SDK：applovin/chartboost/unityads/tapjoy/inmobi/vungle/adcolony/smaato
   - 中文统计/推广：hm.baidu/tongji.baidu/cpro.baidu/pos.baidu/union.baidu/eclick.baidu/cnzz/umeng/51.la
   - 匹配方式改为**精确后缀匹配**（host == d 或 endswith("."+d)），不误伤 myadsystem.com 等

2. **URL 路径特征收紧**：路径段边界匹配（/ads/ /ad- /advert/ /adserver/ /banner/ /promo/ /guanggao/ /tuiguang/ /gg/ 等），避免 /promotional、/adventure 误伤

3. **新增 query 参数特征**：`_has_ad_query()` 只匹配参数名（ad_id/affid/clickid/campaign_id 等），不匹配参数值（from=ad 不误伤）

4. **✅ 新增 HTML 内容层过滤（本次补完，原先缺失）**
   - `filter_html(html)`：剔除广告节点（lxml cssselect 优先，正则降级），内置 80+ 广告 CSS 选择器（div.ad/.adsbox/.advert/.banner-ad、#ad-content、ins.adsbygoogle、iframe/script 广告注入、原生广告、弹窗/浮层、中文小说站广告位）
   - `filter_text(text)`：按行过滤广告文本特征（「广告/推广/赞助」「点击进入/领取」「微信/公众号加群」「下载APP」「扫码关注」等 10+ 特征）
   - `is_ad_image_url(url)`：广告图片位识别（is_ad_url + 图片扩展名 + banner/ads/gg/guanggao 特征）
   - **源级 `extra_css` 支持**：schema §7.5 原先仅文档提及，现已实现（configure 读取 ad_block.extra_css 追加选择器）

5. **m3u8 广告段剔除修复**：pending_dur 记录 EXTINF 时长（修复旧版段行解析时长恒为 None、短段启发式永不生效的问题）

### framework/content.py（接入）
1. `_fetch_chapter_page`：正文纯文本提取后接入 `adblock_for(source).filter_text()`，剔除正文广告行（try/except 包裹，失败不影响正文）
2. `_filter_ad_images`：改为优先走 ad_block 引擎 `is_ad_image_url()`（含域名黑名单/query/源级配置），保留旧版路径特征 + .gif 剔除作兜底

## 兼容性
- 现有接口 `is_ad_url / filter_urls / filter_m3u8 / configure / enabled` 签名不变
- `adblock_for(source)` 单例接口不变
- 新增方法均为增强，未接入时不影响原有行为
- lxml 缺失时 filter_html 自动降级为正则剔除

## 验证
`python _verify_adblock.py` 全部通过：
- is_ad_url 7/7（含误伤防护：from=ad 不误判）
- filter_html：剔除 adsbygoogle/class="ads"，保留正文
- filter_text：剔除「【广告】点击领取福利」行，保留正文
- extra_css：源级 .my-ad/#banner-ad 追加生效
- filter_m3u8：剔除广告段、保留正常段
- py_compile framework/adblock.py framework/content.py 通过
