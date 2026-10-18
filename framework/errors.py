"""统一异常体系。

所有爬虫异常继承 SourceError，GUI 层 catch 它即可捕获所有异常。
每个异常带 source_id，便于按源隔离与展示。
"""


class SourceError(Exception):
    """所有爬虫异常基类。"""

    def __init__(self, message: str, source_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.source_id = source_id

    def to_user_text(self) -> str:
        prefix = f"[{self.source_id}] " if self.source_id else ""
        return f"{prefix}{self.message}"


class ConfigError(SourceError):
    """源配置错误（JSON 缺字段/类型错/冲突）。"""


class SourceNotFoundError(SourceError):
    """按 source_id 找不到源。"""


class RequestError(SourceError):
    """网络请求失败/超时/重试耗尽。"""


class StructureChangedError(SourceError):
    """结构自检失败：站点改版，校验标签未命中。"""


class ContentMissingError(SourceError):
    """详情页解析不到内容（章节/图片/播放地址等）。"""


class DecryptError(SourceError):
    """解密接口不可达或返回异常。"""
