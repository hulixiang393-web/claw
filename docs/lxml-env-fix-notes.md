# 环境修复：lxml 缺失导致 parser fallback（2026-08-09）

## 问题
17k 源制作 subagent 连续两次失败，第2次留下关键线索：
> Parser 在 py-3.13 走 fallback 模式（无 lxml）导致解析全空

## 根因
本机 python（3.11.10，QClaw 自带 D:\Program Files\QClaw\v0.2.35.624\resources\python\python.exe）
**未安装 lxml**。parser.py 的 fallback 模式下：

```python
def _query(self, doc, css=None, xpath=None):
    if not self._lxml:
        return []   # ← fallback 模式 CSS/XPath 查询直接返回空！
```

→ 所有 CSS/XPath 选择器解析全空 → 真机验证时站点内容抓不到，
被误判为「源配置错误」，实际是环境缺依赖。

## 修复
```
python -m pip install lxml          # 安装 6.1.1
python -m pip install cssselect     # lxml.cssselect 依赖，缺失时 cssselect() 抛异常被吞→解析全空
```

验证：
- `python -c "import lxml.html; print(lxml.__version__)"` → 6.1.1
- `python -c "from framework.parser import Parser; print(Parser().engine())"` → lxml
- `python -c "import cssselect; print(cssselect.__version__)"` → 正常

## 完整依赖链（两个缺一不可）
1. **lxml 未装** → parser 走 fallback 正则模式，`_query` 直接返回 [] → 解析全空
2. **lxml 装了但 cssselect 未装** → `doc.cssselect()` 抛 ImportError 被 `_query` 的
   `except Exception: return []` 静默吞掉 → 仍然解析全空
3. 两个都装 → CSS 查询正常（`div.result-card` 等选择器命中）

子代理报的「py-3.13 无 lxml」实际是这两层依赖都缺；本机 3.11.10 同样受影响，
装齐后 avgood 搜索合并测试 7/7 通过。

## 影响
- 所有源（17k/avgood/dm5/haoduoman/manwa/quanben）的真机验证都受此影响
- 修复后 parser 走 lxml 引擎，CSS/XPath 查询正常
- **打包时注意**：build.py 的 PyInstaller 需确认 lxml 被包含（--hidden-import 或依赖自动收集）

## 备注
- fallback 模式仍是「尽力而为」的正则降级，lxml 不可用时部分功能退化
- 若 GUI 打包环境（用户机器）也缺 lxml，需在打包时带上
