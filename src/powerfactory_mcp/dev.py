"""Development harness: runs worker operations without going through MCP.

    python -I -m powerfactory_mcp.dev [--mode parallel] [--calls calls.json] [--py a.py --py b.py] [--op status]

calls.json = [{"op": "...", "args": {...}}, ...]. Prints each reply as JSON (UTF-8).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from powerfactory_mcp.client import WorkerClient
from powerfactory_mcp.config import load_config


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=None)
    ap.add_argument("--calls", action="append", default=[])
    ap.add_argument("--py", action="append", default=[])
    ap.add_argument("--op", action="append", default=[], help="op[:json-args]")
    ap.add_argument("--timeout", type=float, default=None)
    a = ap.parse_args()

    cfg = load_config()
    cfg.idle_release_min = 0
    client = WorkerClient(cfg)
    t = time.perf_counter()
    info = client.start(a.mode)
    print(f"# startup {time.perf_counter() - t:.1f} s", json.dumps(info, ensure_ascii=False)[:400])

    calls = []
    for path in a.calls:
        calls.extend(json.load(open(path, encoding="utf-8")))
    for spec in a.op:
        name, _, js = spec.partition(":")
        calls.append({"op": name, "args": json.loads(js) if js else {}})
    for path in a.py:
        calls.append({"op": "run_python", "args": {"code": open(path, encoding="utf-8").read()}})
    try:
        for c in calls:
            print(f"\n### {c['op']} {json.dumps(c.get('args', {}), ensure_ascii=False)[:200]}")
            r = client.call(c["op"], c.get("args", {}), timeout=a.timeout or c.get("timeout"), long=c.get("long", False))
            if c["op"] == "run_python" and r.get("ok"):
                res = r["result"]
                print(res.get("stdout", ""))
                if res.get("error"):
                    print("ERROR:", res["error"])
                if res.get("result") is not None:
                    print("result =", json.dumps(res["result"], ensure_ascii=False, indent=1))
                print("messages =", json.dumps(r.get("messages"), ensure_ascii=False), flush=True)
            else:
                print(json.dumps(r, ensure_ascii=False, indent=1), flush=True)
    finally:
        client.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
