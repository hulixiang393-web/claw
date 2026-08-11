import json, pathlib, tempfile
from framework.config import load_source
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery

cfg = json.loads(open(r"D:\code\claw\docs\kanman-source-guide.md", encoding="utf-8").read().split("```json")[1].split("```")[0])
tmp = pathlib.Path(tempfile.gettempdir()) / "kanman_test.json"
tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
src = load_source(tmp)
print("source:", src.source_id, src.source_name, "enabled:", src.enabled, "has_discovery:", src.has_discovery())

parser = Parser()
http = HttpClient()
dp = Discovery(http, parser, StructureChecker(http, parser))
works = dp.list_works(src, src.base_url + "/", page=1)
print("WORKS COUNT:", len(works))
for w in works[:5]:
    print(" ", w.title[:24], "|", w.url[:40], "|", w.cover[:50])