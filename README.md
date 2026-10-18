# claw

多源桌面阅读客户端：通用抓取框架 + 源配置 + 缓存/下载 + 阅读器/播放器。

## 功能

- 多视频/漫画源支持，源以 `sources/*.json` 声明，通用解析框架驱动。
- 视频源基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 引擎（高清签名多流、DASH 合并播放）。
- 漫画源 HTML/JSON 解析、封面与章节缓存、阅读进度持久化。
- 本地代理转发（防盗链/广告段过滤）、外部播放器（VLC）集成。

## 快速开始

```bash
pip install -r requirements.txt   # 或按构建脚本准备运行环境
python main.py                    # 启动桌面客户端
```

运行依赖：`yt-dlp`、`ffmpeg`、`VLC`（外部播放器）、Node.js（yt-dlp JS 运行时）。

## 目录

- `framework/` —— 通用框架（抓取、解析、源、流解析、播放器、yt-dlp 引擎）
- `gui/` —— PySide6 桌面客户端与阅读器
- `sources/` —— 各视频/漫画源配置
- `tests/` —— 单元测试与源回归测试

## 文档

完整产品设计、架构、源制作规范与已知限制见 [PROJECT_GUIDE.md](PROJECT_GUIDE.md)。

## 许可

项目内部分发用途，具体许可以仓库设置为准。
