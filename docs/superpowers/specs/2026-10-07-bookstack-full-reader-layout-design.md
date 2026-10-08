# BookStack 全屏阅读布局与正文图片设计

## 目标

- BookStack 阅读器正文占据阅读界面除侧边栏外的全部宽高。
- 多级目录使用树形侧边栏展示任意深度，点击章节在当前阅读器主体切换正文。
- 正文图片通过项目 CoverLoader/HttpClient 下载，并回填 QTextDocument 资源后显示。

## 方案

NovelView 的 BookStack 目录改为 QTreeWidget，左侧宽度约 260px，目录按钮控制侧栏显示/隐藏；正文 QStackedWidget 使用剩余空间。章节节点的 UserRole 保存章节索引，父节点只作为目录分组时不触发正文跳转。

BookStack 正文渲染前提取图片 URL，保留原 HTML 结构。QTextBrowser 关闭自动外链后，通过 QTextDocument.addResource 将图片异步加载结果回填；每次换章递增图片代次，旧章节回调丢弃。加载失败保留正文文字与原有外部链接行为。

## 兼容性

普通源继续使用现有 QListWidget 和文本正文；BookStack 只切换到树形目录与富文本资源加载。Chapter.depth 保留，递归解析顺序不变。
BookStack 阅读卡片固定使用深色正文与白色卡片，不随应用夜间主题反转文字；阅读背景设置作用于卡片外层画布，字号设置作用于卡片正文。

## 阅读界面排版

- 阅读画布使用可配置的浅色背景，正文卡片最大宽度为 960px，水平居中，白底、细边框、8px 圆角和内边距。
- 卡片顶部显示章节标题和来源/文档元信息；正文使用 16px 起始字号、1.8 行高、深色文字、标题层级、段落间距、引用块、代码块、表格和链接样式。
- 图片保持正文结构顺序，下载后按卡片可用宽度缩放、居中显示、圆角处理；窗口宽度变化时重新回填资源，避免大图溢出或裁切。
- BookStack 富文本继续使用站内章节链接跳转；外部链接不在 QTextBrowser 内自动导航。

## 验证

补充树形多级目录、全宽布局、图片资源回填和换章代次测试；运行 BookStack、阅读器相关 pytest、compileall、reader smoke 与 git diff --check。
