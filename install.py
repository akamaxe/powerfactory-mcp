"""PowerFactory MCP server installer for Claude Code (Windows).

    py -3.12 install.py                 # full install into .venv (recommended)
    python install.py --no-venv         # into the active environment (e.g. a conda env with Python 3.12)
    python install.py --dry-run         # show the steps without doing anything

Steps:
  1. Detects PowerFactory and checks that it ships the Python API for this interpreter.
  2. Creates .venv and installs the package in editable mode (a `git pull` updates the code).
  3. Creates the workspace and writes powerfactory-mcp.toml (never overwrites an existing one).
  4. Generates <workspace>\\_ref: attribute catalog and text of the API references.
  5. Copies the "powerfactory" skill to ~\\.claude\\skills and the workspace CLAUDE.md.
  6. Checks that the package and the powerfactory module import.
  7. Registers the server in Claude Code (claude mcp add, user scope).

Uses only the standard library, because it runs before the dependencies are installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
NEEDED = (3, 12)
PY_TAG = f"{sys.version_info.major}.{sys.version_info.minor}"
PF_ROOT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "DIgSILENT"
HOME = Path.home()
SERVER = "powerfactory"
STEPS = 7
DRY = False


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, title: str) -> None:
    say(f"\n[{n}/{STEPS}] {title}")


def fail(msg: str) -> None:
    say(f"\nERROR: {msg}")
    sys.exit(1)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    say("    $ " + subprocess.list2cmdline(cmd))
    if DRY:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, **kw)


def toml_str(value) -> str:
    s = str(value)
    return json.dumps(s) if "'" in s else f"'{s}'"


def inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ 1. PowerFactory
def api_versions(d: Path) -> list[str]:
    return sorted(p.name for p in (d / "Python").glob("*") if (p / "powerfactory.pyd").exists())


def find_powerfactory(arg: str | None) -> Path:
    if arg:
        d = Path(arg)
        if not (d / "Python" / PY_TAG / "powerfactory.pyd").exists():
            fail(f"{d} does not ship the API for Python {PY_TAG} (it has: {', '.join(api_versions(d)) or 'none'}).")
        return d
    installs = sorted((d for d in PF_ROOT.glob("PowerFactory *") if d.is_dir()), key=lambda d: d.name, reverse=True)
    for d in installs:
        if (d / "Python" / PY_TAG / "powerfactory.pyd").exists():
            return d
    found = "; ".join(f"{d.name} (API: {', '.join(api_versions(d)) or 'none'})" for d in installs)
    fail(f"No PowerFactory installation with an API for Python {PY_TAG} found in {PF_ROOT}. "
         f"Installations: {found or 'none'}. Point to the folder with --pf-dir.")


# ------------------------------------------------------------------ 2. Package
def setup_python(a) -> Path:
    if a.no_venv:
        py = Path(sys.executable)
        say(f"    Current environment: {py}")
    else:
        venv = Path(a.venv)
        py = venv / "Scripts" / "python.exe"
        if py.exists():
            say(f"    = {venv} already exists")
        else:
            run([sys.executable, "-m", "venv", venv], check=True)
    if a.skip_install:
        say("    (pip install skipped: --skip-install)")
    else:
        run([py, "-m", "pip", "install", "--disable-pip-version-check", "-e", REPO], check=True)
    return py


# ------------------------------------------------------------------ 3. Workspace
SUBDIRS = ("projects", "results", "backups", "scripts", "logs", "_ref")


def write_config(ws: Path, pf: Path) -> Path:
    for sub in ("", *SUBDIRS):
        if not DRY:
            (ws / sub).mkdir(parents=True, exist_ok=True)
    say(f"    Folders: {ws} ({', '.join(SUBDIRS)})")
    cfg = ws / "powerfactory-mcp.toml"
    if cfg.exists():
        say(f"    = {cfg} already exists: left untouched")
        return cfg
    text = (REPO / "powerfactory-mcp.example.toml").read_text(encoding="utf-8")
    values = {"install_dir": pf, "python_api": pf / "Python" / PY_TAG, "workspace": ws,
              "results_dir": ws / "results", "backups_dir": ws / "backups", "logs_dir": ws / "logs"}
    for key, value in values.items():
        text, n = re.subn(rf"^{key} = '[^']*'", lambda m, k=key, v=value: f"{k} = {toml_str(v)}", text, flags=re.M)
        if n != 1:
            fail(f"The template has no {key} key")
    allowed: list[Path] = []
    cands = (HOME / "Documents", HOME / "Downloads", ws, Path(tempfile.gettempdir()) / "claude")
    for p in sorted(cands, key=lambda p: len(str(p))):  # folders contained in another one are redundant
        if not any(inside(p, q) for q in allowed):
            allowed.append(p)
    block = "allowed = [\n" + "".join(f"    {toml_str(p)},\n" for p in allowed) + "]"
    text, n = re.subn(r"^allowed = \[.*?\]", lambda m: block, text, flags=re.M | re.S)
    if n != 1:
        fail("The template has no allowed key")
    if not DRY:
        cfg.write_text(text, encoding="utf-8")
    say(f"    + {cfg}")
    return cfg


# ------------------------------------------------------------------ 5. Skill and CLAUDE.md
def same_tree(a: Path, b: Path) -> bool:
    fa = {p.relative_to(a): p for p in a.rglob("*") if p.is_file()}
    fb = {p.relative_to(b): p for p in b.rglob("*") if p.is_file()}
    return fa.keys() == fb.keys() and all(fa[k].read_bytes() == fb[k].read_bytes() for k in fa)


def install_skill(skills_dir: Path, ws: Path, force: bool) -> None:
    src = REPO / "skill" / "powerfactory"
    dst = skills_dir / "powerfactory"
    if dst.exists():
        if same_tree(src, dst):
            say(f"    = {dst} is already up to date")
            return
        if not force:
            say(f"    ! {dst} already exists and differs: left as is (--force replaces it, with a backup)")
            return
        # The backup goes outside skills\, so that Claude Code does not load two skills with the same name.
        bak = ws / "backups" / f"skill-powerfactory-{time.strftime('%Y%m%d-%H%M%S')}"
        say(f"    Backup of the previous skill: {bak}")
        if not DRY:
            shutil.move(str(dst), str(bak))
    if not DRY:
        shutil.copytree(src, dst)
    say(f"    + {dst}")


def install_workspace_notes(ws: Path) -> None:
    dst = ws / "CLAUDE.md"
    if dst.exists():
        say(f"    = {dst} already exists: left untouched")
        return
    if not DRY:
        shutil.copyfile(REPO / "workspace" / "CLAUDE.md", dst)
    say(f"    + {dst}")


# ------------------------------------------------------------------ 6. Check
CHECK = ("import os, sys; os.add_dll_directory(sys.argv[1]); sys.path.insert(0, sys.argv[2]); "
         "import asyncio, powerfactory, powerfactory_mcp.server as s; "
         "print('powerfactory', powerfactory.__version__, '| MCP tools:', len(asyncio.run(s.mcp.list_tools())))")


def check(py: Path, pf: Path, cfg: Path) -> bool:
    env = {**os.environ, "PF_MCP_CONFIG": str(cfg)}
    r = run([py, "-I", "-c", CHECK, pf, pf / "Python" / PY_TAG], env=env, capture_output=True, text=True)
    if DRY:
        return True
    out = (r.stdout + r.stderr).strip()
    say("    " + (out.splitlines()[-1] if out else f"exit code {r.returncode}"))
    return r.returncode == 0


# ------------------------------------------------------------------ 7. Registration
def register(py: Path, cfg: Path, scope: str, force: bool) -> None:
    claude = shutil.which("claude")
    add = [claude or "claude", "mcp", "add", "-s", scope, SERVER, "-e", f"PF_MCP_CONFIG={cfg}",
           "--", py, "-I", "-m", "powerfactory_mcp"]
    if not claude:
        say("    ! The `claude` command was not found on the PATH. Register the server manually:")
        say("      " + subprocess.list2cmdline([str(c) for c in add]))
        return
    exists = subprocess.run([claude, "mcp", "get", SERVER], capture_output=True, text=True).returncode == 0
    if exists and not force:
        say(f"    = a '{SERVER}' server is already registered: left as is (--force re-registers it)")
        return
    if exists:
        run([claude, "mcp", "remove", SERVER, "-s", scope])
    r = run(add, capture_output=True, text=True)
    if r.returncode != 0:
        say(f"    ! claude mcp add failed: {(r.stdout + r.stderr).strip()}")


def main() -> int:
    global DRY
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pf-dir", help="PowerFactory folder (default: the most recent one with an API for this Python)")
    ap.add_argument("--workspace", default=str(HOME / "Documents" / "PowerFactory"),
                    help="workspace: configuration, results, backups (default %(default)s)")
    ap.add_argument("--venv", default=str(REPO / ".venv"), help="virtual environment to create (default %(default)s)")
    ap.add_argument("--no-venv", action="store_true", help="install into this interpreter's environment")
    ap.add_argument("--skip-install", action="store_true", help="do not run pip install (package already installed)")
    ap.add_argument("--skip-ref", action="store_true", help="do not generate <workspace>\\_ref")
    ap.add_argument("--skip-skill", action="store_true", help="do not copy the skill or the CLAUDE.md")
    ap.add_argument("--skip-register", action="store_true", help="do not register the server in Claude Code")
    ap.add_argument("--skills-dir", default=str(HOME / ".claude" / "skills"), help="Claude Code skills folder")
    ap.add_argument("--scope", default="user", choices=["user", "local", "project"], help="MCP registration scope")
    ap.add_argument("--force", action="store_true", help="replace the skill (with a backup) and re-register")
    ap.add_argument("--dry-run", action="store_true", help="show the steps without running them")
    a = ap.parse_args()
    DRY = a.dry_run

    if sys.platform != "win32":
        fail("PowerFactory only runs on Windows.")
    if sys.version_info[:2] != NEEDED:
        fail(f"Python {NEEDED[0]}.{NEEDED[1]} is required (this is {PY_TAG}). Run: py -3.12 install.py")
    ws = Path(a.workspace).expanduser().resolve()
    say(f"powerfactory-mcp installer{' (dry run: --dry-run)' if DRY else ''}\n  Repository: {REPO}")

    step(1, "PowerFactory")
    pf = find_powerfactory(a.pf_dir)
    say(f"    {pf} (Python API {PY_TAG})")

    step(2, "powerfactory-mcp package")
    py = setup_python(a)

    step(3, "Workspace and configuration")
    cfg = write_config(ws, pf)

    step(4, "Local references (_ref)")
    if a.skip_ref:
        say("    (skipped: --skip-ref)")
    else:
        r = run([py, "-I", "-m", "powerfactory_mcp.setup_ref"], env={**os.environ, "PF_MCP_CONFIG": str(cfg)})
        if r.returncode != 0:
            say("    ! Some references were not generated; the server works anyway.")

    step(5, "Skill and workspace notes")
    if a.skip_skill:
        say("    (skipped: --skip-skill)")
    else:
        install_skill(Path(a.skills_dir), ws, a.force)
        install_workspace_notes(ws)

    step(6, "Check")
    if not check(py, pf, cfg):
        fail("The package could not be imported; check the messages above.")

    step(7, "Claude Code registration")
    if a.skip_register:
        say("    (skipped: --skip-register)")
    else:
        register(py, cfg, a.scope, a.force)

    env_hint = "" if cfg == HOME / "Documents" / "PowerFactory" / "powerfactory-mcp.toml" else \
        f"\n      (with this workspace, first run: set PF_MCP_CONFIG={cfg})"
    say(f"""
Done.
  - Configuration: {cfg}
  - Open Claude Code (or use /mcp if it was already open) and check that "{SERVER}" shows as connected.
  - Engine test without Claude Code (parallel mode, does not touch your database):
      "{py}" -I -m powerfactory_mcp.dev --mode parallel --op status{env_hint}
  - To avoid confirming every tool, in ~\\.claude\\settings.json:
      "permissions": {{"allow": ["mcp__{SERVER}"],
                      "ask": ["mcp__{SERVER}__run_python", "mcp__{SERVER}__delete_objects"]}}""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
