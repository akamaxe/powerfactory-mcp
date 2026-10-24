"""Worker process: hosts the PowerFactory engine and serves operations over a named pipe.

Everything runs on the main thread (the PowerFactory API requires a single thread). Launched by client.py:
    python -I -m powerfactory_mcp.worker --address \\\\.\\pipe\\... --mode exclusive --config <toml>
The authentication key arrives in the PFMCP_AUTHKEY environment variable.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
import traceback
from ctypes import wintypes
from multiprocessing.connection import Client

from powerfactory_mcp.config import load_config


def pump_messages() -> None:
    """Dispatches the thread's Win32 messages so the engine's visible window does not freeze."""
    user32 = ctypes.windll.user32
    msg = wintypes.MSG()
    for _ in range(200):
        if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True)
    ap.add_argument("--mode", choices=("exclusive", "parallel"), required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--show-gui", action="store_true")
    a = ap.parse_args()
    # -I ignores PYTHONIOENCODING: the log is written in UTF-8 explicitly.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    conn = Client(a.address, family="AF_PIPE", authkey=bytes.fromhex(os.environ.pop("PFMCP_AUTHKEY")))
    cfg = load_config(a.config)
    log(f"worker pid={os.getpid()} mode={a.mode} python={sys.version.split()[0]}")

    sys.path.insert(0, str(cfg.python_api))
    try:
        os.add_dll_directory(str(cfg.install_dir))
    except OSError:
        pass

    try:
        import powerfactory as pf
    except Exception as e:  # noqa: BLE001
        conn.send({"event": "error", "message": f"Could not import powerfactory from {cfg.python_api}: {e}"})
        return 2

    args = []
    if a.mode == "parallel":
        args.append(f"/inMemory /instance {cfg.parallel_instance}")
    if cfg.extra_args:
        args.append(cfg.extra_args)
    t0 = time.perf_counter()
    try:
        app = pf.GetApplicationExt(cfg.username or None, cfg.password or None, " ".join(args) or None)
    except pf.ExitError as e:
        log("ExitError", e.code, e)
        conn.send({"event": "error", "code": e.code, "message": str(e)})
        return 3
    except Exception as e:  # noqa: BLE001
        log("GetApplicationExt failed", repr(e))
        conn.send({"event": "error", "message": repr(e)})
        return 3
    log(f"PowerFactory {pf.__version__} started in {time.perf_counter() - t0:.1f} s")

    from powerfactory_mcp import ops
    from powerfactory_mcp.ops import OPS, OpError
    from powerfactory_mcp.ops.common import Ctx, jsonable

    ops.load_all()
    ctx = Ctx(app, pf, cfg, a.mode)
    try:
        app.SetUserBreakEnabled(0)  # the break button slows simulations down and there is no user
    except Exception:  # noqa: BLE001
        pass
    if a.show_gui:
        try:
            app.Show()
            ctx.gui_visible = ctx.gui_initialized = True
        except Exception as e:  # noqa: BLE001
            log("Show() failed:", e)

    info = OPS["status"](ctx)
    info["startup_s"] = round(time.perf_counter() - t0, 1)
    conn.send({"event": "ready", "info": jsonable(info, ctx)})

    exit_code = 0
    try:
        while True:
            try:
                ready = conn.poll(0.05 if ctx.gui_visible else 0.25)
            except (OSError, EOFError):
                log("pipe closed: the server exited")
                break
            if not ready:
                if ctx.gui_visible:
                    pump_messages()
                continue
            try:
                msg = conn.recv()
            except (OSError, EOFError):
                log("pipe closed: the server exited")
                break
            rid, name, kwargs = msg.get("id"), msg.get("op"), msg.get("args") or {}
            if name == "__shutdown__":
                conn.send({"id": rid, "ok": True})
                log("orderly shutdown")
                break
            reply = {"id": rid}
            fn = OPS.get(name)
            t = time.perf_counter()
            if fn is None:
                reply.update(ok=False, error=f"Unknown operation: {name}")
            else:
                ctx.begin_op()
                try:
                    reply.update(ok=True, result=jsonable(fn(ctx, **kwargs), ctx))
                except OpError as e:
                    reply.update(ok=False, error=str(e))
                except pf.ExitError as e:
                    reply.update(ok=False, error=f"PowerFactory exited (ExitError {e.code}: {e}). "
                                                 "The next call restarts it.")
                    exit_code = 4
                except TypeError as e:
                    reply.update(ok=False, error=f"Invalid arguments for {name}: {e}")
                except Exception as e:  # noqa: BLE001
                    reply.update(ok=False, error=f"{type(e).__name__}: {e}",
                                 traceback=traceback.format_exc(limit=8))
                reply["messages"] = ctx.collect_messages()
                try:
                    reply["context"] = ctx.brief()
                except Exception:  # noqa: BLE001
                    pass
            reply["elapsed_s"] = round(time.perf_counter() - t, 2)
            log(f"{name} -> {'ok' if reply.get('ok') else 'error'} ({reply['elapsed_s']} s)")
            try:
                conn.send(reply)
            except (OSError, EOFError):
                log("could not send the reply: the server exited")
                break
            except Exception as e:  # noqa: BLE001 - e.g. something not serializable
                conn.send({"id": rid, "ok": False, "error": f"Reply not serializable: {e}"})
            if exit_code:
                break
    finally:
        try:
            app.WriteChangesToDb()
        except Exception:  # noqa: BLE001
            pass
        # Drop every reference to app: destroying it shuts PowerFactory down and releases the local database.
        ctx.py_ns = None
        ctx.app = ctx.ow = None
        del ctx, app
        import gc
        gc.collect()
        log("PowerFactory closed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
