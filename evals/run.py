#!/usr/bin/env python3
"""Run every Handle eval and aggregate the result.

    python3 evals/run.py

Each eval prints its own scorecard and exits non-zero on any FAIL; this runner
reports a per-suite line and exits non-zero if any suite failed. Live checks
that depend on open tabs / the extension / a debug port SKIP rather than fail.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["read_eval.py", "chrome_data_eval.py", "mcp_eval.py", "bridge_eval.py", "workflow_eval.py"]

failed = []
print("Running Handle eval suite\n")
for s in SUITES:
    path = HERE / s
    if not path.exists():
        print(f"  ⚠ {s} — missing"); continue
    p = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
    summary = next((l.strip() for l in p.stdout.splitlines() if "—" in l and "pass" in l), "(no summary)")
    mark = "✅" if p.returncode == 0 else "❌"
    print(f"  {mark} {s:24} {summary}")
    if p.returncode != 0:
        failed.append(s)
        # surface the failing lines
        for l in p.stdout.splitlines():
            if l.strip().startswith("❌"):
                print(f"        {l.strip()}")

print()
if failed:
    print(f"FAILED: {', '.join(failed)}  — run that suite directly for detail.")
    sys.exit(1)
print("All suites green.")
