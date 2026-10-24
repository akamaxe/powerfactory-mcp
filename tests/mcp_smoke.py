"""End-to-end test over the MCP protocol (stdio), the same way Claude Code uses it.

    python -I tests\\mcp_smoke.py                                   # session, projects and error path
    python -I tests\\mcp_smoke.py --project "My project" --case "Base case" --bus "Bus 5" [--rms] [--diagram]

Launches the server with the same interpreter (or --python) and the configuration from PF_MCP_CONFIG (or --config).
Uses parallel mode: it does not modify the real database. Returns the number of failures.
"""

import argparse
import asyncio
import os
import sys
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def build_calls(a) -> list[tuple[str, dict, bool]]:
    """(tool, arguments, error expected)."""
    calls = [
        ("status", {}, False),
        ("connect", {"mode": "parallel"}, False),
        ("list_projects", {"contains": a.project} if a.project else {}, False),
    ]
    if a.project:
        calls += [
            ("open_project", {"project": a.project, "study_case": a.case}, False),
            ("project_overview", {}, False),
            ("run_load_flow", {"report": "summary"}, False),
            ("run_short_circuit", {"fault_type": "3ph", "locations": [a.bus] if a.bus else "busbars", "limit": 20},
             False),
            ("run_python", {"code": "result = len(find('*.ElmTerm'))"}, False),
            ("list_protection", {"limit": 5}, False),
        ]
        if a.rms and a.bus:
            calls += [
                ("run_dynamic_simulation", {"t_end": 5.0, "events": [
                    {"type": "short_circuit", "target": a.bus, "time": 0.1},
                    {"type": "clear", "target": a.bus, "time": 0.2}], "monitor": "auto", "plot": False,
                    "export": False}, False),
            ]
        if a.diagram:
            calls.append(("export_diagram", {}, False))
    calls += [
        ("describe_object", {"object": "nonexistent-object-xyz"}, True),
        ("status", {}, False),
        ("disconnect", {}, False),
        ("status", {}, False),
    ]
    return calls


async def run(a) -> int:
    env = {"PF_MCP_CONFIG": a.config} if a.config else {}
    params = StdioServerParameters(command=a.python, args=["-I", "-m", "powerfactory_mcp"], env=env)
    failures = 0
    async with stdio_client(params) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            print(f"tools: {len(tools.tools)}", flush=True)
            for name, args, expect_error in build_calls(a):
                t = time.time()
                r = await s.call_tool(name, args, read_timeout_seconds=900)
                is_err = bool(getattr(r, "is_error", getattr(r, "isError", False)))
                text = "".join(getattr(c, "text", "") for c in r.content)
                ok = is_err == expect_error
                failures += not ok
                print(f"## {'ok ' if ok else 'FAIL'} {name} error={is_err} ({time.time() - t:.1f} s): {text[:500]}",
                      flush=True)
                if '"job_id"' in text and not is_err:  # a long study became a job: wait for it
                    r = await s.call_tool("jobs", {"action": "wait", "wait_s": 600}, read_timeout_seconds=900)
                    is_err = bool(getattr(r, "is_error", getattr(r, "isError", False)))
                    failures += is_err
                    text = "".join(getattr(c, "text", "") for c in r.content)
                    print(f"## {'FAIL' if is_err else 'ok '} jobs wait: {text[:300]}", flush=True)
    print("FAILURES:", failures)
    return failures


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default=sys.executable, help="interpreter with powerfactory_mcp installed")
    ap.add_argument("--config", default=os.environ.get("PF_MCP_CONFIG"), help="powerfactory-mcp.toml")
    ap.add_argument("--project", help="project to open (name or path from list_projects)")
    ap.add_argument("--case", help="study case")
    ap.add_argument("--bus", help="bus for the short circuit and the RMS fault")
    ap.add_argument("--rms", action="store_true", help="include a short RMS simulation (needs --bus)")
    ap.add_argument("--diagram", action="store_true", help="include export_diagram (the first run takes minutes)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
