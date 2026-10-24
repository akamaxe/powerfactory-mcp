"""run_python: runs arbitrary Python code inside the engine (escape hatch for everything without a typed tool)."""

from __future__ import annotations

import contextlib
import io
import traceback

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops import common

MAX_STDOUT = 20000


def _namespace(ctx) -> dict:
    if ctx.py_ns is None:
        ctx.py_ns = {
            "__name__": "__pfmcp__",
            "app": ctx.app,
            "pf": ctx.pf,
            "ctx": ctx,
            # helpers
            "find": lambda pattern, scope="calc", include_oos=True: common.find(ctx, pattern, scope, include_oos),
            "get": lambda spec, cls=None: common.resolve_one(ctx, spec, cls),
            "attr": common.get_attr,
            "oname": common.oname,
            "out_path": ctx.out_path,
            "write_table": lambda path, columns, rows: str(common.write_table(ctx, ctx.cfg.check_path(path),
                                                                              columns, rows)),
        }
    return ctx.py_ns


@op()
def run_python(ctx, code: str, reset: bool = False) -> dict:
    """Runs `code` with app, pf and helpers in the namespace (persistent between calls).

    To return data, assign it to `result` (converted to JSON). print() is captured.
    """
    if reset:
        ctx.py_ns = None
    ns = _namespace(ctx)
    ns["result"] = None
    buf = io.StringIO()
    try:
        compiled = compile(code, "<run_python>", "exec")
    except SyntaxError as e:
        raise OpError(f"SyntaxError: {e}") from None
    error = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            exec(compiled, ns)  # noqa: S102 - this is what the tool is for
        except ctx.pf.ExitError:
            raise
        except Exception:  # noqa: BLE001
            error = traceback.format_exc(limit=6)
    out = buf.getvalue()
    if len(out) > MAX_STDOUT:
        out = (out[:MAX_STDOUT // 2] + f"\n... ({len(out) - MAX_STDOUT} characters omitted) ...\n"
               + out[-MAX_STDOUT // 2:])
    res = {"stdout": out, "result": common.jsonable(ns.get("result"), ctx)}
    if error:
        res["error"] = error
    return res
