"""Worker process client: startup, calls with timeouts, jobs and idle release.

The worker connects back over an authenticated named pipe (multiprocessing.connection),
so its stdout/stderr go to a log and nothing PowerFactory prints pollutes the MCP stdio.
"""

from __future__ import annotations

import ctypes
import itertools
import msvcrt
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from multiprocessing.connection import Client, Listener
from typing import TextIO

from powerfactory_mcp.config import Config

CREATE_NO_WINDOW = 0x08000000

# Most likely powerfactory.ExitError codes (see <workspace>\_ref\ErrorCodeReference_en.txt).
EXIT_CODES = {
    3000: "No PowerFactory licence found (is CodeMeter running, dongle/softkey present?).",
    3008: "The licence is an engine licence: Show() is not available.",
    3010: "The licence does not include 'Scripting and Automation'.",
    3013: "The CodeMeter.exe service is not running.",
    3018: "The licence's maximum number of users has been reached.",
    4002: "Another PowerFactory instance is already using the local database (is the GUI or another engine open?). "
          "Close it or use connect(mode='parallel').",
    7000: "PowerFactory cannot start twice in the same process.",
}


class WorkerError(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class _WorkerDied(Exception):
    pass


@dataclass
class Job:
    id: str
    op: str
    args: dict
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    reply: dict | None = None
    error: str | None = None
    finished: float | None = None

    def summary(self) -> dict:
        end = self.finished or time.monotonic()
        state = "running" if not self.done.is_set() else ("error" if self.error else "done")
        return {"job_id": self.id, "op": self.op, "status": state, "elapsed_s": round(end - self.started, 1)}


def gui_processes() -> list[int]:
    """PIDs of running PowerFactory.exe processes (the user's GUI; the MCP engine runs inside python.exe)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq PowerFactory.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == "powerfactory.exe" and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def open_append(path) -> TextIO:
    """Opens the log with OS-level append (FILE_APPEND_DATA only): every write lands at the end even when
    several servers and their workers write at once. Python's 'a' mode only appends within its own process; the
    handle inherited by the worker writes at its own position and overwrites other sessions' lines."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    file_append_data, synchronize, share_all, open_always = 0x0004, 0x00100000, 0x7, 4
    h = k32.CreateFileW(str(path), file_append_data | synchronize, share_all, None, open_always, 0x80, None)
    if h is None or h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return open(msvcrt.open_osfhandle(h, os.O_APPEND), "a", encoding="utf-8")


class WorkerClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._conn = None
        self._listener = None
        self.mode: str | None = None
        self.gui_visible = False
        self.info: dict = {}
        self.started_at: float | None = None
        self.last_activity = time.monotonic()
        self.last_release: str | None = None
        self.jobs: dict[str, Job] = {}
        self._job: Job | None = None
        self._seq = itertools.count(1)
        self.log_path = cfg.logs_dir / "worker.log"
        threading.Thread(target=self._idle_loop, name="pf-idle", daemon=True).start()

    # ------------------------------------------------------------------- state
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._conn is not None

    def _check_busy(self) -> None:
        j = self._job
        if j and not j.done.is_set():
            raise Busy(
                f"PowerFactory is busy with job {j.id} ({j.op}, {time.monotonic() - j.started:.0f} s). "
                f"Use jobs(action='wait', job_id='{j.id}') or jobs(action='cancel')."
            )

    def state(self) -> dict:
        d = {
            "connected": self.alive,
            "mode": self.mode if self.alive else None,
            "default_mode": self.cfg.default_mode,
            "gui_visible": self.gui_visible if self.alive else False,
            "worker_pid": self._proc.pid if self.alive else None,
            "uptime_s": round(time.time() - self.started_at) if self.alive and self.started_at else None,
            "idle_s": round(time.monotonic() - self.last_activity),
            "idle_release_min": self.cfg.idle_release_min,
            "last_release": self.last_release,
            "user_gui_pids": gui_processes(),
            "running_job": self._job.summary() if self._job and not self._job.done.is_set() else None,
            "log": str(self.log_path),
            "workspace": str(self.cfg.workspace),
            "config": str(self.cfg.source),
        }
        return d

    # ---------------------------------------------------------------- startup
    def start(self, mode: str | None = None, show_gui: bool | None = None) -> dict:
        with self._lock:
            self._check_busy()
            return self._start_locked(mode or self.cfg.default_mode,
                                      self.cfg.show_gui if show_gui is None else show_gui)

    def _start_locked(self, mode: str, show_gui: bool) -> dict:
        if mode not in ("exclusive", "parallel"):
            raise WorkerError(f"Invalid mode: {mode!r} (exclusive | parallel)")
        if self.alive:
            if mode == self.mode:
                return self.info
            self._stop_locked(f"mode change to {mode}")
        self._cleanup_locked()
        if mode == "exclusive":
            pids = gui_processes()
            if pids:
                raise WorkerError(
                    f"PowerFactory GUI is open (PID {', '.join(map(str, pids))}). In exclusive mode the local database "
                    "allows only one instance: close the GUI or use connect(mode='parallel') (in-memory copy)."
                )
        self.cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        self._rotate_log()

        key = secrets.token_bytes(32)
        address = rf"\\.\pipe\pfmcp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        listener = Listener(address=address, family="AF_PIPE", authkey=key)
        env = dict(os.environ, PFMCP_AUTHKEY=key.hex(), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        cmd = [sys.executable, "-I", "-m", "powerfactory_mcp.worker",
               "--address", address, "--mode", mode, "--config", str(self.cfg.source)]
        if show_gui:
            cmd.append("--show-gui")
        with open_append(self.log_path) as log:
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start mode={mode} =====\n")
            log.flush()
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                    creationflags=CREATE_NO_WINDOW)
        try:
            conn = self._accept(listener, address, proc, timeout=60)
            deadline = time.monotonic() + self.cfg.startup_s
            while True:
                if conn.poll(1.0):
                    msg = conn.recv()
                    if msg.get("event") == "ready":
                        break
                    if msg.get("event") == "error":
                        raise WorkerError(self._explain(msg))
                elif proc.poll() is not None:
                    raise WorkerError(f"The worker exited during startup (code {proc.returncode}). "
                                      f"Log tail:\n{self._log_tail()}")
                elif time.monotonic() > deadline:
                    raise WorkerError(f"PowerFactory did not start within {self.cfg.startup_s:.0f} s. "
                                      f"Log tail:\n{self._log_tail()}")
        except BaseException:
            self._terminate(proc)
            listener.close()
            raise
        self._proc, self._conn, self._listener = proc, conn, listener
        self.mode, self.gui_visible = mode, show_gui
        self.info = msg.get("info", {})
        self.started_at = time.time()
        self.last_release = None
        self.last_activity = time.monotonic()
        return self.info

    @staticmethod
    def _accept(listener, address, proc, timeout):
        box: dict = {}

        def run():
            try:
                box["conn"] = listener.accept()
            except Exception as e:  # noqa: BLE001 - reported below
                box["error"] = e

        t = threading.Thread(target=run, name="pf-accept", daemon=True)
        t.start()
        end = time.monotonic() + timeout
        while t.is_alive():
            t.join(0.25)
            if t.is_alive() and (proc.poll() is not None or time.monotonic() > end):
                try:  # unblock accept() with a wrong-key client
                    Client(address, family="AF_PIPE", authkey=b"x")
                except Exception:  # noqa: BLE001
                    pass
                t.join(5)
                raise WorkerError("The worker did not connect to the control pipe.")
        if "conn" not in box:
            raise WorkerError(f"Failed to accept the worker connection: {box.get('error')}")
        return box["conn"]

    def _explain(self, msg: dict) -> str:
        code = msg.get("code")
        text = msg.get("message", "")
        if code in EXIT_CODES:
            return f"PowerFactory did not start (ExitError {code}): {EXIT_CODES[code]}"
        if code:
            return (f"PowerFactory did not start (ExitError {code}: {text}). "
                    f"See {self.cfg.workspace / '_ref' / 'ErrorCodeReference_en.txt'}")
        return f"PowerFactory did not start: {text}"

    # --------------------------------------------------------------- shutdown
    def stop(self, reason: str = "disconnect") -> None:
        with self._lock:
            self._check_busy()
            self._stop_locked(reason)

    def _stop_locked(self, reason: str) -> None:
        if self.alive:
            try:
                self._conn.send({"id": 0, "op": "__shutdown__", "args": {}})
                if self._conn.poll(60):
                    self._conn.recv()
            except (OSError, EOFError):
                pass
            try:
                self._proc.wait(60)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._cleanup_locked()
        self.last_release = reason

    def kill(self, reason: str) -> None:
        """Kills the worker without waiting (to cancel jobs or on timeouts)."""
        proc = self._proc
        if proc is not None:
            self._terminate(proc)
        self.last_release = reason

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                pass

    def _cleanup_locked(self) -> None:
        for obj in (self._conn, self._listener):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        if self._proc is not None and self._proc.poll() is None:
            self._terminate(self._proc)
        self._proc = self._conn = self._listener = None
        self.gui_visible = False

    def shutdown(self) -> None:
        """On MCP server exit: orderly shutdown if possible, otherwise kill."""
        if self._job and not self._job.done.is_set():
            self.kill("server shutdown with a job running")
        acquired = self._lock.acquire(timeout=5)
        try:
            self._stop_locked("MCP server shutdown")
        finally:
            if acquired:
                self._lock.release()

    # ------------------------------------------------------------------- calls
    def call(self, op: str, args: dict | None = None, *, timeout: float | None = None,
             long: bool = False, autostart: bool = True) -> dict:
        with self._lock:
            self._check_busy()
            if not self.alive:
                if not autostart:
                    raise WorkerError("PowerFactory is not connected. Use connect().")
                self._start_locked(self.cfg.default_mode, self.cfg.show_gui)
            rid = next(self._seq)
            self.last_activity = time.monotonic()
            try:
                self._conn.send({"id": rid, "op": op, "args": args or {}})
            except (OSError, EOFError) as e:
                self._died_locked()
                raise WorkerError(f"Lost the connection to the worker ({e}). Try again.") from e
            wait = self.cfg.inline_wait_s if long else (timeout or self.cfg.default_s)
            try:
                reply = self._recv(rid, wait)
            except _WorkerDied as e:
                self._died_locked()
                raise WorkerError(str(e)) from None
            if reply is not None:
                self.last_activity = time.monotonic()
                if op == "show_gui" and reply.get("ok"):
                    self.gui_visible = bool((reply.get("result") or {}).get("visible"))
                return reply
            if not long:
                self.kill(f"{wait:.0f} s timeout in {op}")
                self._cleanup_locked()
                raise WorkerError(
                    f"'{op}' exceeded {wait:.0f} s and the PowerFactory worker was restarted "
                    "(changes already written to the database are kept; the next call starts it again)."
                )
            job = Job(id=f"job{rid}", op=op, args=args or {}, started=time.monotonic() - wait)
            self.jobs[job.id] = job
            self._job = job
            limit = timeout or self.cfg.job_max_s
            threading.Thread(target=self._job_waiter, args=(job, rid, limit), name=f"pf-{job.id}",
                             daemon=True).start()
            return {"ok": True, "job": job.summary(),
                    "note": f"Still running in PowerFactory. Check with jobs(action='wait', job_id='{job.id}')."}

    def _recv(self, rid: int, wait: float) -> dict | None:
        """Waits for reply `rid`; None on timeout. If the worker dies, _WorkerDied (without cleanup)."""
        deadline = time.monotonic() + wait
        conn, proc = self._conn, self._proc
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                if conn.poll(min(remaining, 1.0)):
                    msg = conn.recv()
                    if msg.get("id") == rid:
                        return msg
                    continue
            except (OSError, EOFError) as e:
                raise _WorkerDied(f"The PowerFactory worker exited unexpectedly ({e}). "
                                  f"Log tail:\n{self._log_tail()}") from None
            if proc is None or proc.poll() is not None:
                raise _WorkerDied("The PowerFactory worker exited unexpectedly. "
                                  f"Log tail:\n{self._log_tail()}")

    def _died_locked(self) -> None:
        self._cleanup_locked()
        self.last_release = "the worker exited unexpectedly"

    def _job_waiter(self, job: Job, rid: int, limit: float) -> None:
        proc = self._proc
        try:
            reply = self._recv(rid, max(limit - (time.monotonic() - job.started), 1))
            if reply is None:
                self.kill(f"job {job.id} exceeded {limit:.0f} s")
                job.error = f"Exceeded the {limit:.0f} s limit; the worker was killed."
            else:
                job.reply = reply
        except _WorkerDied as e:
            job.error = str(e)
        finally:
            if job.error:
                # Clean up before done.set(): that way no new call starts a worker that then gets killed here.
                with self._lock:
                    if self._proc is proc:
                        self._cleanup_locked()
            job.finished = time.monotonic()
            self.last_activity = time.monotonic()
            job.done.set()

    def wait_job(self, job_id: str | None, wait_s: float) -> dict:
        job = self._find_job(job_id)
        job.done.wait(max(0.0, wait_s))
        out = job.summary()
        if job.done.is_set():
            out["reply"] = job.reply
            out["error"] = job.error
        return out

    def cancel_job(self, job_id: str | None) -> dict:
        job = self._find_job(job_id)
        if job.done.is_set():
            return {**job.summary(), "note": "The job had already finished."}
        self.kill(f"job {job.id} cancelled")
        job.done.wait(30)
        return {**job.summary(),
                "note": "The PowerFactory worker was killed; the next call starts it again."}

    def list_jobs(self) -> list[dict]:
        return [j.summary() for j in self.jobs.values()]

    def _find_job(self, job_id: str | None) -> Job:
        if job_id:
            if job_id not in self.jobs:
                raise WorkerError(f"Job {job_id} does not exist. Jobs: {', '.join(self.jobs) or 'none'}")
            return self.jobs[job_id]
        if self._job is None:
            raise WorkerError("There are no jobs.")
        return self._job

    # -------------------------------------------------- idle release and logs
    def _idle_loop(self) -> None:
        while True:
            time.sleep(30)
            limit = self.cfg.idle_release_min * 60
            if limit <= 0 or not self.alive:
                continue
            if self._job and not self._job.done.is_set():
                continue
            if time.monotonic() - self.last_activity < limit:
                continue
            if self._lock.acquire(timeout=1):
                try:
                    if self.alive and time.monotonic() - self.last_activity >= limit:
                        self._stop_locked(f"idle release (> {self.cfg.idle_release_min:g} min)")
                finally:
                    self._lock.release()

    def _rotate_log(self) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 5 * 2**20:
                old = self.log_path.with_suffix(".log.1")
                if old.exists():
                    old.unlink()
                self.log_path.rename(old)
        except OSError:
            pass

    def _log_tail(self, n: int = 25) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return "(no log)"
