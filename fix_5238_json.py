# -*- coding: utf-8 -*-
"""给 5238.json 加 playwright 兜底配置（保留原 description 不变）。"""
import json

p = "sources/5238.json"
d = json.load(open(p, encoding="utf-8"))
ep = d["endpoints"]["content"]["episode"]["play_url"]
ep["render"] = "playwright"
ep["render_config"] = {
    "wait_until": "networkidle",
    "timeout_ms": 45000,
    "extra_delay_ms": 10000,
}
# 追加说明到 description（用 \n 转义，保持 JSON 合法）
desc = d["$metadata"]["description"]
desc += "\n【2026-08-24 全站 Cloudflare 拦截】全站被 Cloudflare Just a moment 质询硬拦(HTTP 403)。方案1: 真实浏览器过验证后把 cf_clearance 写入 data/cf_cookies.json, 框架自动带上。方案2: play_url.render=playwright 兜底(真实 Chromium 过 challenge 后取 video source m3u8, 约20~40s)。"
d["$metadata"]["description"] = desc
open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2))
print("done. render =", ep.get("render"), "| http_chain steps:", len(ep.get("http_chain", [])))
