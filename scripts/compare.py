"""把多次 `gua eval` 产生的 summary-*.json 汇总成一张对比表（Markdown）。

用法: python scripts/compare.py runs/summary-*.json
"""
import json
import sys
from pathlib import Path

COLS = ["runs", "success_rate", "false_done_rate", "recovery_rate", "avg_steps", "avg_calls", "avg_tokens", "avg_seconds"]

rows = []
for f in sys.argv[1:]:
    d = json.loads(Path(f).read_text(encoding="utf-8"))
    tag = Path(f).stem.replace("summary-", "").rsplit("-", 2)[0]
    rows.append((tag, d["summary"]))
print("| config | " + " | ".join(COLS) + " |")
print("|---" * (len(COLS) + 1) + "|")
for tag, s in rows:
    print(f"| {tag} | " + " | ".join("-" if s.get(c) is None else str(s.get(c)) for c in COLS) + " |")
