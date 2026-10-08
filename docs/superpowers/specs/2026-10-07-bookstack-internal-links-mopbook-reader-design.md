# BookStack 站内目录跳转与 MopBook 阅读链路设计

## 目标

1. BookStack 正文中的站内章节链接在应用阅读器内跳转，不打开系统浏览器。
2. BookStack 二级目录在阅读器左侧目录中保留层级缩进，并能点击加载对应正文。
3. MopBook 详情页与章节页沿用统一的 curl_cffi/Chrome 指纹请求策略，详情成功后进入小说阅读器；请求被站点验证或网络失败时显示可诊断错误，不跳转外部浏览器。

## 设计

### BookStack 章节模型

扩展章节目录项的可选层级信息，保持现有 `Chapter(title, url)` 调用兼容。BookStack 解析器递归遍历 `ul/ol > li`，为顶层和嵌套章节记录 `depth`，同时保持 DOM 顺序和 URL 去重。普通源继续生成深度为 0 的章节。

### 阅读器正文链接

将 BookStack 的 `QTextBrowser` 从自动打开外部链接改为链接点击信号处理。点击链接时：

- 归一化绝对 URL，匹配当前详情的章节 URL；
- 匹配成功则调用现有 `_load_chapter(index)`；
- 当前书籍域名下但未匹配章节的链接，仍使用系统浏览器，避免误拦截参考链接；
- 外部链接和图片资源保持原有外部打开行为。

章节正文继续通过当前阅读器视图渲染，不改变缓存、进度和图片加载逻辑。

### 左侧目录

`QListWidgetItem` 保存章节索引和深度。BookStack 项目按深度设置缩进和显示文本；点击行为仍走 `_load_chapter()`。非 BookStack 项目保持现有样式。

### MopBook 请求链路

不绕过 WAF/验证码。统一详情、章节和发现请求使用项目 `HttpClient` 的 curl_cffi Chrome 指纹、UA、Referer、重试和挑战识别。MopBook 配置保持相对 URL，进入 `fetch_detail()` 后校验章节 URL 已绝对化。详情请求失败时由 GUI 异步任务返回错误并停留在发现页，显示具体请求/验证提示；成功后直接调用阅读器加载。

## 测试

- BookStack fixture 覆盖一级/二级目录顺序、depth、重复链接去重。
- 阅读器测试正文站内章节链接调用 `_load_chapter()`，外部链接调用浏览器打开。
- MopBook fixture 覆盖详情、章节 URL 绝对化和正文解析。
- 运行相关 pytest、compileall、GUI reader smoke 和 `git diff --check`。
