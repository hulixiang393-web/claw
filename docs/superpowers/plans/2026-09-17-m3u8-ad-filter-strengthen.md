# 视频 m3u8 广告过滤加强 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 加强视频 m3u8 广告过滤——新增协议级广告标签识别（R4）、片头/片尾预滚识别（R5）、中插离群块识别（R6），并让视频流在播放（阅读）与下载两条链路默认启用内置规则。

**Architecture:** `framework/adblock.py` 的 `detect_m3u8_ads` 扩展判定规则（保留 R1/R2/R3 + 测试锁定）；`AdblockEngine.configure` 增加 `default_on` 参数；下载 3 处 + 播放 2 处挂接点传 `default_on=True` 或改守卫。全部离线测试，无真实网络。

**Tech Stack:** Python（urllib.parse / re / lxml 可选降级），pytest。

## Global Constraints

- 范围仅视频 HLS（m3u8）流内广告；**小说/漫画路径不动**（`filter_text` / `_filter_ad_images` 保持原行为）。
- R1/R2/R3 既有规则与现有测试**不得改变行为**（`tests/test_adblock_html.py` 现有用例必须继续全绿）。
- 新规则全部「双信号」或协议级标签：R5/R6 必须同时满足两个条件才判广告，防止误删正片分片。
- 异常静默：任何新规则解析失败走既有 try/except，返回原文，不过滤不阻断播放/下载。
- 无新增第三方依赖；lxml 缺失时既有降级路径不变。
- 无 lint 配置（无 flake8/ruff/pyproject）。测试命令：
  ```powershell
  $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_adblock_html.py -q
  ```
- 每次 Task 提交前先 `git status` 确认只 stage 本次任务相关文件；不做任何与本计划无关的改动。
- 源显式 `ad_block.enabled: false` 仍必须能关闭过滤（所有新增 default_on 路径都要尊重）。

---

### Task 1: 引擎默认策略——configure/adblock_for 支持 default_on

**Files:**
- Modify: `framework/adblock.py:257-298`（`__init__`/`configure`）与 `framework/adblock.py:553-560`（`adblock_for`）
- Test: `tests/test_adblock_html.py`（追加 `TestDefaultOn` 类）

**Interfaces:**
- Produces: `AdblockEngine(source=None, default_on=False)`、`AdblockEngine.configure(source, default_on=False)`、`adblock_for(source=None, default_on=False)`。语义：源无 `ad_block` 时 `_enabled = default_on`；有 `ad_block` 时 `_enabled = bool(ad.get("enabled", True))` 不变。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_adblock_html.py` 末尾）

```python
class TestDefaultOn:
    class _NoAdBlock:
        raw = {"content_type": "video"}

    class _Disabled:
        raw = {"ad_block": {"enabled": False}}

    def test_source_without_adblock_disabled_by_default(self):
        assert AdblockEngine(self._NoAdBlock()).enabled is False

    def test_source_without_adblock_default_on(self):
        eng = AdblockEngine(self._NoAdBlock(), default_on=True)
        assert eng.enabled is True
        assert eng.is_ad_url("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js")

    def test_configure_default_on_param(self):
        eng = AdblockEngine()
        eng.configure(self._NoAdBlock(), default_on=True)
        assert eng.enabled is True

    def test_explicit_enabled_false_respected(self):
        assert AdblockEngine(self._Disabled(), default_on=True).enabled is False

    def test_adblock_for_default_on(self):
        from framework.adblock import adblock_for

        assert adblock_for(self._NoAdBlock(), default_on=True).enabled is True
        assert adblock_for(self._NoAdBlock()).enabled is False
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestDefaultOn" -q`
Expected: FAIL（TypeError: `__init__()` got an unexpected keyword argument 'default_on'）

- [ ] **Step 3: 实现**

`__init__` 改为（注意新增 `self._extra_css = []` 初始行，保证无 ad_block 源也能安全调用 `filter_html`）：

```python
    def __init__(self, source=None, default_on: bool = False):
        self._block_re = _DEFAULT_URL_AD_RE
        self._block_domains = list(_DEFAULT_AD_DOMAINS)
        self._enabled = True
        self._extra_regexes: List[re.Pattern] = []
        self._extra_domains: List[str] = []
        self._extra_css: List[str] = []
        if source is not None:
            self.configure(source, default_on=default_on)
```

`configure` 签名与「未配置 ad_block」分支改为：

```python
    def configure(self, source, default_on: bool = False) -> None:
        """从源配置读 ad_block，构建过滤规则。

        default_on：源未配置 ad_block 时的默认开关。默认 False（源作者没
        声明要去广告就走关闭路径）；视频流路径传 True（内置启发式默认生效，
        片头/中插广告在无源级配置时也过滤）。
        """
        # 重置补充规则：同一实例被 configure 多次时不累积旧规则
        self._extra_regexes = []
        self._extra_domains = []
        raw = getattr(source, "raw", None) or {}
        ad = raw.get("ad_block") or {}
        # 源未配置 ad_block → 按 default_on 决定是否启用内置规则
        if not raw.get("ad_block"):
            self._enabled = default_on
            return
        self._enabled = bool(ad.get("enabled", True))
        if not self._enabled:
            return
```

`adblock_for` 改为：

```python
def adblock_for(source=None, default_on: bool = False) -> AdblockEngine:
    """取某源的广告引擎（读源 ad_block 配置）。缺省用内置规则。

    default_on：源未配置 ad_block 时是否启用内置规则（视频流路径传 True）。
    """
    if source is not None:
        return AdblockEngine(source, default_on=default_on)
    global _default_engine
    if _default_engine is None:
        _default_engine = AdblockEngine()
    return _default_engine
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestDefaultOn" -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 回归本测试文件**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_adblock_html.py -q`
Expected: PASS（既有 + 新增全绿）

- [ ] **Step 6: Commit**

```bash
git add framework/adblock.py tests/test_adblock_html.py
git commit -m "feat(adblock): configure/adblock_for 支持 default_on——视频流可默认启用内置规则"
```

---

### Task 2: R4 协议级广告标签识别（CUE-OUT/CUE-IN、SCTE35、DATERANGE）

**Files:**
- Modify: `framework/adblock.py:457-488`（`detect_m3u8_ads` 第二遍块解析，追加标签计数）与 `framework/adblock.py:489-500`（第三遍 R3 之后并入 `seg_cue`）
- Test: `tests/test_adblock_html.py`（追加 `TestM3u8CueTags` 类）

**Interfaces:**
- Produces: 局部变量 `seg_cue: set`（处于广告标签区间内的段序号，0 起），第三遍 `ad_set |= seg_cue`。

- [ ] **Step 1: 写失败测试**（追加 `tests/test_adblock_html.py` 末尾）

```python
class TestM3u8CueTags:
    def test_cue_out_in_removed(self):
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            "#EXTINF:5.0,\n/seg/a1.ts\n"
            "#EXT-X-CUE-OUT:30.0\n"
            "#EXTINF:5.0,\n/seg/ad1.ts\n"
            "#EXTINF:5.0,\n/seg/ad2.ts\n"
            "#EXT-X-CUE-IN\n"
            "#EXTINF:5.0,\n/seg/a2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/index.m3u8")
        assert "ad1.ts" not in out and "ad2.ts" not in out
        assert "a1.ts" in out and "a2.ts" in out

    def test_dangling_cue_out_until_end(self):
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-CUE-OUT:10.0\n"
            "#EXTINF:5.0,\n/seg/ad1.ts\n"
            "#EXTINF:5.0,\n/seg/ad2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert "ad1.ts" not in out and "ad2.ts" not in out

    def test_scte35_out_in_removed(self):
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            "#EXTINF:5.0,\n/seg/a1.ts\n"
            "#EXT-X-SCTE35-OUT\n"
            "#EXTINF:5.0,\n/seg/ad1.ts\n"
            "#EXT-X-SCTE35-IN\n"
            "#EXTINF:5.0,\n/seg/a2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert "ad1.ts" not in out
        assert "a1.ts" in out and "a2.ts" in out

    def test_legacy_scte35_splice(self):
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            "#EXTINF:5.0,\n/seg/a1.ts\n"
            '#EXT-X-SCTE35:CUE="ad_id=1000, am_splice_type=0x02"\n'
            "#EXTINF:5.0,\n/seg/ad1.ts\n"
            '#EXT-X-SCTE35:CUE="am_splice_type=0x0e"\n'
            "#EXTINF:5.0,\n/seg/a2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert "ad1.ts" not in out
        assert "a1.ts" in out and "a2.ts" in out

    def test_daterange_ad_marker(self):
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            '#EXT-X-DATERANGE:ID="ad1",CLASS="com.example.ad",X-ASSET="/ad",SCTE35-OUT=0xFC00\n'
            "#EXTINF:5.0,\n/seg/ad1.ts\n"
            "#EXT-X-CUE-IN\n"
            "#EXTINF:5.0,\n/seg/a1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert "ad1.ts" not in out
        assert "a1.ts" in out
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8CueTags" -q`
Expected: FAIL（`ad1.ts` 仍在输出中）

- [ ] **Step 3: 实现**——替换 `detect_m3u8_ads` 第二遍块解析段（当前 `# ---- 第二遍` 注释到 `short_block_segs` 计算之前），加入 R4 标签计数：

```python
        # ---- 第二遍：按 DISCONTINUITY 划分块 + 跟踪协议级广告标签区间 ----
        # 块 = 一段连续段序列；块前紧邻 DISCONTINUITY 视为「独立块」。
        # 独立块且块内段总时长 <3s → 块内所有段判广告（孤立短块，R3）。
        # 列表开头的块（前无 DISCONTINUITY）不判——整列表即短视频时不受影响。
        # R4：服务器自带广告声明标签（CUE-OUT/SCTE35/DATERANGE），区间内段判广告。
        blocks: List[dict] = []
        cur: Optional[dict] = None
        seg_idx = -1
        cue_depth = 0
        seg_cue: set = set()
        for line in lines:
            # —— R4 协议级广告标签（标签行本身不是段，continue 不干扰段解析）——
            if line.startswith("#EXT-X-CUE-OUT") or line.startswith("#EXT-X-SCTE35-OUT"):
                cue_depth += 1
                continue
            if line.startswith("#EXT-X-CUE-IN") or line.startswith("#EXT-X-SCTE35-IN"):
                cue_depth = max(0, cue_depth - 1)
                continue
            if line.startswith("#EXT-X-SCTE35:"):
                # 遗留单标签：placement_opportunity(0x2/0x02) 开启，0xe/0x0e 结束
                if re.search(r"am_splice_type=0x0?2\b", line):
                    cue_depth += 1
                elif re.search(r"am_splice_type=0x0?e\b", line):
                    cue_depth = max(0, cue_depth - 1)
                continue
            if line.startswith("#EXT-X-DATERANGE:") and (
                "SCTE35" in line or "X-AD" in line or "X-ASSET" in line
            ):
                cue_depth += 1
                continue
            if line.startswith("#EXT-X-DISCONTINUITY"):
                if cur is not None and cur["seg"]:
                    blocks.append(cur)
                cur = {"seg": [], "dur": 0.0, "independent": True}
                continue
            m = re.match(r"#EXTINF:\s*([\d.]+)", line)
            if m:
                pending_dur = float(m.group(1))
                continue
            if line and not line.startswith("#"):
                seg_idx += 1
                if cur is None:
                    cur = {"seg": [], "dur": 0.0, "independent": False}
                dur = pending_dur
                pending_dur = None
                cur["seg"].append(seg_idx)
                cur["dur"] += dur if dur is not None else 0.0
                if cue_depth > 0:
                    seg_cue.add(seg_idx)
        if cur is not None and cur["seg"]:
            blocks.append(cur)
```

并在第三遍 `ad_set |= short_block_segs` 之后追加一行：

```python
        # R4: 协议级广告标签区间（CUE-OUT/CUE-IN、SCTE35、DATERANGE 广告标记）
        ad_set |= seg_cue
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8CueTags" -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 回归既有 m3u8 用例**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8StreamAds" "tests/test_adblock_html.py::TestBackwardCompat" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add framework/adblock.py tests/test_adblock_html.py
git commit -m "feat(adblock): R4 协议级广告标签识别——CUE-OUT/CUE-IN、SCTE35、DATERANGE 区间内段剔除"
```

---

### Task 3: R5 片头/片尾预滚识别

**Files:**
- Modify: `framework/adblock.py`（`detect_m3u8_ads` 第二遍追加 `durs` 记录；第三遍 R4 之后追加总时长/uniform/首位块判定）
- Test: `tests/test_adblock_html.py`（追加 `TestM3u8PreRoll` 类）

**Interfaces:**
- Consumes: Task 2 的 `seg_cue`、`pending_dur` 消费逻辑不变
- Produces: R5 规则——首位/末位块，整体 `total_dur >= 60.0` 且 `b["dur"] < 0.25 * total_dur` 且（`b["dur"] <= 30.0` 或 `uniform`）→ 广告。

- [ ] **Step 1: 写失败测试**（追加 `tests/test_adblock_html.py` 末尾）

```python
class TestM3u8PreRoll:
    def test_leading_short_uniform_block_removed(self):
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            + "".join(f"#EXTINF:5.0,\n/seg/ad{i}.ts\n" for i in range(1, 4))
            + "#EXT-X-DISCONTINUITY\n"
            + "".join(f"#EXTINF:10.0,\n/seg/{i:03d}.ts\n" for i in range(1, 11))
            + "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert "ad1.ts" not in out and "ad3.ts" not in out
        segs = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
        assert segs == [f"/seg/{i:03d}.ts" for i in range(1, 11)]

    def test_trailing_short_uniform_block_removed(self):
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            + "".join(f"#EXTINF:10.0,\n/seg/{i:03d}.ts\n" for i in range(1, 11))
            + "#EXT-X-DISCONTINUITY\n"
            + "".join(f"#EXTINF:5.0,\n/seg/ad{i}.ts\n" for i in range(1, 4))
            + "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert "ad1.ts" not in out and "ad3.ts" not in out
        segs = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
        assert segs == [f"/seg/{i:03d}.ts" for i in range(1, 11)]

    def test_leading_block_not_short_kept(self):
        # 首位块占 50%（30s/60s）> 25% → 保留
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            + "".join(f"#EXTINF:10.0,\n/seg/f{i}.ts\n" for i in range(1, 4))
            + "#EXT-X-DISCONTINUITY\n"
            + "".join(f"#EXTINF:10.0,\n/seg/c{i}.ts\n" for i in range(1, 4))
            + "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert out == m3u8

    def test_short_video_first_block_kept(self):
        # 整体 25s < 60s → 预滚不启用（防短视频误删）
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            "#EXTINF:5.0,\n/seg/a1.ts\n"
            "#EXT-X-DISCONTINUITY\n"
            "#EXTINF:10.0,\n/seg/b1.ts\n"
            "#EXTINF:10.0,\n/seg/b2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert out == m3u8
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8PreRoll" -q`
Expected: FAIL（`ad1.ts` 仍在输出中）

- [ ] **Step 3: 实现**——第二遍块追加 `durs` 列表（在 `cur = {...}` 两处初始化与 `cur["dur"] += ...` 之后追加）：

```python
                cur["seg"].append(seg_idx)
                cur["dur"] += dur if dur is not None else 0.0
                if dur is not None:
                    cur["durs"].append(dur)
                if cue_depth > 0:
                    seg_cue.add(seg_idx)
```

`cur` 两处初始化改为 `{"seg": [], "dur": 0.0, "durs": [], "independent": True}` / `{"seg": [], "dur": 0.0, "durs": [], "independent": False}`。

第三遍做块级判定。在 `ad_set |= seg_cue` 之后追加：

```python
        # ---- R5：片头/片尾预滚（双信号：块占整体 <25% + 短 或 块内时长全一致） ----
        total_dur = sum(d for _joined, d in seg_infos if d is not None)
        for bi, _b in enumerate(blocks):
            _segs = _b["seg"]
            if not _segs:
                continue
            _is_first = bi == 0
            _is_last = bi == len(blocks) - 1
            _uniform = len(_b["durs"]) >= 2 and all(
                _d == _b["durs"][0] for _d in _b["durs"]
            )
            if (
                (_is_first or _is_last)
                and total_dur >= 60.0
                and _b["dur"] < 0.25 * total_dur
                and (_b["dur"] <= 30.0 or _uniform)
            ):
                ad_set.update(_segs)
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8PreRoll" -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 回归全部既有 m3u8/默认用例**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8CueTags" "tests/test_adblock_html.py::TestM3u8StreamAds" "tests/test_adblock_html.py::TestBackwardCompat" "tests/test_adblock_html.py::TestDefaultOn" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add framework/adblock.py tests/test_adblock_html.py
git commit -m "feat(adblock): R5 片头/片尾预滚识别——短+均匀双信号，整体>=60s 才启用"
```

---

### Task 4: R6 中插离群块识别

**Files:**
- Modify: `framework/adblock.py`（`detect_m3u8_ads` 第三遍 R5 之后追加块时长中位数 + 内部块离群判定）
- Test: `tests/test_adblock_html.py`（追加 `TestM3u8MidRoll` 类）

**Interfaces:**
- Consumes: Task 3 的 `blocks`（含 `durs`）、`total_dur`
- Produces: R6 规则——内部块（非首非尾）`b["dur"] < median/3` 且（`uniform` 或 `b["dur"] < 10.0`）→ 广告。

- [ ] **Step 1: 写失败测试**（追加 `tests/test_adblock_html.py` 末尾）

```python
class TestM3u8MidRoll:
    def test_short_uniform_interior_block_removed(self):
        eng = AdblockEngine()
        content = "".join(f"#EXTINF:10.0,\n/seg/c{i}.ts\n" for i in range(1, 5))
        m3u8 = (
            "#EXTM3U\n" + content
            + "#EXT-X-DISCONTINUITY\n"
            "#EXTINF:4.0,\n/seg/m1.ts\n"
            "#EXTINF:4.0,\n/seg/m2.ts\n"
            "#EXT-X-DISCONTINUITY\n" + content
            + "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert "m1.ts" not in out and "m2.ts" not in out
        assert "c1.ts" in out

    def test_longer_varied_interior_block_kept(self):
        # 12s 内部块（8+4 非均匀），各块中位数 80s：12 < 80/3 成立但无均匀签名
        # 且 12 > 10 → 保留
        eng = AdblockEngine()
        content = "".join(f"#EXTINF:20.0,\n/seg/c{i}.ts\n" for i in range(1, 5))
        m3u8 = (
            "#EXTM3U\n" + content
            + "#EXT-X-DISCONTINUITY\n"
            "#EXTINF:8.0,\n/seg/m1.ts\n"
            "#EXTINF:4.0,\n/seg/m2.ts\n"
            "#EXT-X-DISCONTINUITY\n" + content
            + "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8")
        assert out == m3u8
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8MidRoll" -q`
Expected: FAIL（`m1.ts` 仍在输出中）

- [ ] **Step 3: 实现**——在第三遍 R5 追加代码之后（`_is_last = ...` 循环外面）追加：

```python
        # ---- R6：中插离群块（内部块 + 块时长远小于中位数 + 均匀/超短） ----
        block_durs = sorted(_b["dur"] for _b in blocks if _b["seg"])
        if block_durs:
            mid = len(block_durs) // 2
            median_dur = (
                block_durs[mid]
                if len(block_durs) % 2 == 1
                else (block_durs[mid - 1] + block_durs[mid]) / 2.0
            )
        else:
            median_dur = 0.0
        for bi, _b in enumerate(blocks):
            _segs = _b["seg"]
            if not _segs:
                continue
            _uniform = len(_b["durs"]) >= 2 and all(
                _d == _b["durs"][0] for _d in _b["durs"]
            )
            if (
                0 < bi < len(blocks) - 1  # 非首非尾（前后都有 DISCONTINUITY）
                and median_dur > 0.0
                and _b["dur"] < median_dur / 3.0
                and (_uniform or _b["dur"] < 10.0)
            ):
                ad_set.update(_segs)
```

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest "tests/test_adblock_html.py::TestM3u8MidRoll" -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 回归全部 adblock 用例**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_adblock_html.py -q`
Expected: PASS（既有 + 全部新增）

- [ ] **Step 6: Commit**

```bash
git add framework/adblock.py tests/test_adblock_html.py
git commit -m "feat(adblock): R6 中插离群块识别——内部块短于中位数1/3且均匀或<10s 判广告"
```

---

### Task 5: 下载链路默认启用（downloader + download_queue）

**Files:**
- Modify: `framework/downloader.py:414`（`_filter_m3u8_for_download`）与 `framework/downloader.py:478`（`_download_hls`）；`framework/download_queue.py:224`（`_spawn_ad_precheck` 内）
- Test: `tests/test_downloader_ad_default.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `adblock_for(source, default_on=True)`
- Produces: 无 ad_block 的视频源下载时也会剔除 m3u8 广告段；显式 `enabled:false` 仍关闭。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_downloader_ad_default.py`）

```python
# -*- coding: utf-8 -*-
"""下载路径 m3u8 广告过滤默认启用（无 ad_block 源也生效）。"""
from framework.downloader import Downloader


_AD_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-CUE-OUT\n"
    "#EXTINF:5.0,\n/seg/ad1.ts\n"
    "#EXTINF:5.0,\n/seg/ad2.ts\n"
    "#EXT-X-CUE-IN\n"
    "#EXTINF:10.0,\n/seg/001.ts\n"
    "#EXT-X-ENDLIST\n"
)


class _FakeHttp:
    def get_text(self, url, headers=None, timeout=0, retries=0):
        return _AD_M3U8


class _NoAdBlockSource:
    raw = {"content_type": "video"}

    def request_headers(self):
        return {"Referer": "https://src.example/"}


class _DisabledSource:
    raw = {"ad_block": {"enabled": False}, "content_type": "video"}

    def request_headers(self):
        return {"Referer": "https://src.example/"}


def test_filter_m3u8_for_download_default_on(tmp_path):
    d = Downloader(content=None, http=_FakeHttp(), settings=None)
    out = d._filter_m3u8_for_download(
        _NoAdBlockSource(), "https://cdn.example.com/hls/i.m3u8", tmp_path
    )
    assert out is not None
    txt = out.read_text(encoding="utf-8")
    assert "ad1.ts" not in txt and "ad2.ts" not in txt
    assert "001.ts" in txt


def test_filter_m3u8_for_download_respects_disabled(tmp_path):
    d = Downloader(content=None, http=_FakeHttp(), settings=None)
    out = d._filter_m3u8_for_download(
        _DisabledSource(), "https://cdn.example.com/hls/i.m3u8", tmp_path
    )
    assert out is None
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_downloader_ad_default.py -q`
Expected: FAIL（`test_filter_m3u8_for_download_default_on`——ad.enabled False → 返回 None）

- [ ] **Step 3: 实现**——三处 `adblock_for(source)` 改为 `adblock_for(source, default_on=True)`：

- `framework/downloader.py` `_filter_m3u8_for_download` 内（原 `from .adblock import adblock_for` 之后）
- `framework/downloader.py` `_download_hls` 内
- `framework/download_queue.py` `_spawn_ad_precheck` 的 `_precheck` 内

即把：

```python
            ad = adblock_for(source)
```

改为：

```python
            ad = adblock_for(source, default_on=True)
```

（`_download_hls` 与预检的 `if not ad.enabled: return None / return` 守卫保留——显式 `enabled:false` 仍关闭。）

同时更新 `_download_hls` 内现有过期注释（downloader.py:473-474「无配置时跳过不误删」改为「无 ad_block 源也启用内置规则；源显式 enabled:false 才关闭」），`_filter_m3u8_for_download` 无此注释无需改。

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_downloader_ad_default.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add framework/downloader.py framework/download_queue.py tests/test_downloader_ad_default.py
git commit -m "feat(download): 无 ad_block 视频源下载也默认启用 m3u8 广告段剔除"
```

---

### Task 6: 播放链路默认启用（media_proxy + external_player）

**Files:**
- Modify: `framework/media_proxy.py:282`、`framework/media_proxy.py:308`（`if ad_block:` 守卫）与 `framework/media_proxy.py:355-370`（`_filter_ad_segments`）；`framework/external_player.py:63-76`（走代理分支）
- Test: `tests/test_media_proxy_ad.py` 与 `tests/test_external_player_hls.py`（新建）

**Interfaces:**
- Consumes: Task 1 的引擎默认启用语义
- Produces: 播放时 HLS 一律走本地代理并由内置规则剔除广告段；`ad_block.enabled:false` 仍关闭；无防盗链头的 mp4 仍直连。

- [ ] **Step 1: 写失败测试**

`tests/test_media_proxy_ad.py`：

```python
# -*- coding: utf-8 -*-
"""播放代理 m3u8 广告过滤：无配置也用内置规则；显式关闭仍生效。"""
from framework.media_proxy import MediaProxy


_AD_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-CUE-OUT\n"
    "#EXTINF:5.0,\n/seg/ad1.ts\n"
    "#EXT-X-CUE-IN\n"
    "#EXTINF:10.0,\n/seg/001.ts\n"
    "#EXT-X-ENDLIST\n"
)


def test_filter_ad_segments_no_config_builtin():
    mp = MediaProxy.instance()
    out = mp._filter_ad_segments(_AD_M3U8, "https://cdn.example.com/hls/i.m3u8", None)
    assert "ad1.ts" not in out
    assert "001.ts" in out


def test_filter_ad_segments_respects_disabled():
    mp = MediaProxy.instance()
    out = mp._filter_ad_segments(
        _AD_M3U8, "https://cdn.example.com/hls/i.m3u8", {"enabled": False}
    )
    assert out == _AD_M3U8
```

`tests/test_external_player_hls.py`：

```python
# -*- coding: utf-8 -*-
"""外部播放器：HLS 一律走本地代理（无防盗链头也走），非 HLS 无头仍直连。"""
import framework.external_player as ep


def _play(monkeypatch, url, headers):
    calls = []

    def _proxy(u, *a, **k):
        calls.append(u)
        return "PROXY:" + u

    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\Program Files\VideoLAN\VLC\vlc.exe")
    monkeypatch.setattr(ep, "proxy_url_for", _proxy)
    monkeypatch.setattr(ep.subprocess, "Popen", lambda *a, **k: None)
    return ep.open_with_player(
        url, audio="", referer="", user_agent="", headers=headers, ad_block=None
    ), calls


def test_hls_goes_through_proxy_without_headers(monkeypatch):
    _, calls = _play(monkeypatch, "https://cdn.example.com/hls/index.m3u8", {})
    assert "https://cdn.example.com/hls/index.m3u8" in calls


def test_mp4_without_headers_direct(monkeypatch):
    _, calls = _play(monkeypatch, "https://cdn.example.com/movie.mp4", {})
    assert calls == []


def test_hls_audio_slave_proxied(monkeypatch):
    calls = {}

    def _proxy(u, *a, **k):
        calls[u] = True
        return "PROXY:" + u

    monkeypatch.setattr(ep, "_locate_vlc", lambda: r"C:\Program Files\VideoLAN\VLC\vlc.exe")
    monkeypatch.setattr(ep, "proxy_url_for", _proxy)
    monkeypatch.setattr(ep.subprocess, "Popen", lambda *a, **k: None)
    ep.open_with_player(
        "https://cdn.example.com/hls/index.m3u8",
        audio="https://cdn.example.com/hls/a.m3u8",
        referer="", user_agent="", headers={}, ad_block=None,
    )
    assert "https://cdn.example.com/hls/index.m3u8" in calls
    assert "https://cdn.example.com/hls/a.m3u8" in calls
```

- [ ] **Step 2: 运行确认失败**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_media_proxy_ad.py tests/test_external_player_hls.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`framework/media_proxy.py` 两处（`_forward` 内两个 m3u8 分支）守卫：

```python
                if ad_block:
```
→
```python
                if ad_block is not None:
```

`_filter_ad_segments` 改为（无配置时 `AdblockEngine()` 默认启用内置规则）：

```python
    def _filter_ad_segments(self, m3u8_text: str, base_url: str, ad_block: dict) -> str:
        """按源 ad_block 配置剔除 m3u8 广告段（播放路径广告过滤）。

        复用 adblock 引擎的 filter_m3u8（URL 广告特征 + 重复段 + 孤立短块 +
        CUE/SCTE35 标签 + 片头尾/中插离群启发式，与下载路径一致）。
        ad_block 为空/None → 内置规则默认启用；ad_block.enabled=false → 关闭。
        失败/异常返回原文（不过滤不阻断播放）。
        """
        try:
            from .adblock import AdblockEngine
            engine = AdblockEngine()
            if ad_block:
                engine.configure(type("S", (), {"raw": {"ad_block": ad_block}})())
            if engine.enabled:
                return engine.filter_m3u8(m3u8_text, base_url)
        except Exception:  # noqa: BLE001 —— 过滤失败不阻断播放
            pass
        return m3u8_text
```

`framework/external_player.py` `open_with_player` 的代理分支（原 `if hdrs:`）改为：

```python
        # HLS 流一律走本地代理：即使无防盗链头也要让代理剔除 m3u8 广告段
        # （片头/中插广告在播放路径过滤）；其余媒体有防盗链头才走代理。
        is_hls = url.split("?", 1)[0].lower().endswith(".m3u8")
        if hdrs or is_hls:
            # 带防盗链头或 HLS → 本地代理（代理打完整 headers + 广告过滤）
            play_url = proxy_url_for(url, hdrs, ad_block=ad_block)
            audio_url = proxy_url_for(audio, hdrs, ad_block=ad_block) if audio else ""
        else:
            play_url = url
            audio_url = audio
```

文档字符串中「任何防盗链头存在时走本地代理」相应补充「HLS 流一律走本地代理（含广告段过滤）」。

- [ ] **Step 4: 运行确认通过**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_media_proxy_ad.py tests/test_external_player_hls.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add framework/media_proxy.py framework/external_player.py tests/test_media_proxy_ad.py tests/test_external_player_hls.py
git commit -m "feat(play): HLS 播放一律走本地代理且默认启用 m3u8 广告段过滤"
```

---

### Task 7: 全量回归与收尾

**Files:**
- 无新增改动（仅验证）

**Interfaces:**
- Consumes: 前 6 个 Task 全部完成

- [ ] **Step 1: 定向回归**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/test_adblock_html.py tests/test_downloader_ad_default.py tests/test_media_proxy_ad.py tests/test_external_player_hls.py -q`
Expected: PASS

- [ ] **Step 2: 全量测试**

Run: `$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/ -q -x`
Expected: 全部通过（无回归）；若有既有 flaky（如已知 `tests/test_comic_view_referer.py` 时序问题），用 `--ignore=tests/test_comic_view_referer.py` 重跑并记录。

- [ ] **Step 3: py_compile 语法校验**

Run: `$env:PYTHONIOENCODING="utf-8"; python -m py_compile framework/adblock.py framework/downloader.py framework/download_queue.py framework/media_proxy.py framework/external_player.py`
Expected: 无输出（退出码 0）

- [ ] **Step 4: 记录知识沉淀**（全局协议：项目收口必做）

在 `d:/code/claw/SESSION.md` 追加本次会话记录（改动摘要、测试结果、阈值决策）；按需更新 `docs/adblock-notes.md` / `docs/video-ad-removal-notes.md` 追加 R4/R5/R6 与 default_on 说明。

- [ ] **Step 5: 最终状态确认**

`git status --short` 应只包含本计划的修改与测试文件（不含 `sources/fanqie.json.bak-fanqie-categories` 等无关 untracked）。确认后向用户汇报 commit 列表。