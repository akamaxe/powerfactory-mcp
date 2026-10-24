"""Configuration loading (TOML) and allowed-path checks."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_VAR = "PF_MCP_CONFIG"
DEFAULT_WORKSPACE = Path.home() / "Documents" / "PowerFactory"
DEFAULT_CONFIG = DEFAULT_WORKSPACE / "powerfactory-mcp.toml"
PF_ROOT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "DIgSILENT"
PY_TAG = f"{sys.version_info.major}.{sys.version_info.minor}"


def detect_install_dir(py_tag: str = PY_TAG, root: Path = PF_ROOT) -> Path | None:
    """Most recent PowerFactory that ships the Python API for this interpreter version."""
    for d in sorted(root.glob("PowerFactory *"), key=lambda p: p.name, reverse=True):
        if (d / "Python" / py_tag / "powerfactory.pyd").exists():
            return d
    return None


class PathNotAllowed(ValueError):
    pass


@dataclass
class Config:
    install_dir: Path
    python_api: Path
    username: str = ""
    password: str = ""
    default_mode: str = "exclusive"
    parallel_instance: str = "CLAUDE"
    show_gui: bool = False
    extra_args: str = ""
    workspace: Path = DEFAULT_WORKSPACE
    results_dir: Path = DEFAULT_WORKSPACE / "results"
    backups_dir: Path = DEFAULT_WORKSPACE / "backups"
    logs_dir: Path = DEFAULT_WORKSPACE / "logs"
    allowed: list[Path] = field(default_factory=list)
    startup_s: float = 240
    default_s: float = 300
    inline_wait_s: float = 90
    job_max_s: float = 14400
    idle_release_min: float = 20
    source: Path | None = None

    def check_path(self, p: str | os.PathLike, *, must_exist: bool = False) -> Path:
        """Returns the absolute path if it falls inside `allowed`; otherwise PathNotAllowed."""
        path = Path(p).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = Path(os.path.normpath(path))
        resolved = path.resolve(strict=False)
        for root in self.allowed:
            try:
                resolved.relative_to(root.resolve(strict=False))
                break
            except ValueError:
                continue
        else:
            roots = ", ".join(str(r) for r in self.allowed)
            raise PathNotAllowed(
                f"Path outside the allowed ones: {path}. Allowed: {roots} "
                f"(extend them in [paths].allowed of {self.source})"
            )
        if must_exist and not path.exists():
            raise FileNotFoundError(f"Does not exist: {path}")
        return path

    def to_dict(self) -> dict:
        d = {}
        for k, v in self.__dict__.items():
            if k == "password":
                d[k] = "***" if v else ""
            elif isinstance(v, Path):
                d[k] = str(v)
            elif isinstance(v, list):
                d[k] = [str(x) for x in v]
            else:
                d[k] = v
        return d


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get(ENV_VAR) or DEFAULT_CONFIG)
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    pf = data.get("powerfactory", {})
    paths = data.get("paths", {})
    to = data.get("timeouts", {})

    # No install_dir/python_api in the TOML: the most recent installation with an API for this Python.
    if pf.get("install_dir"):
        install_dir = Path(pf["install_dir"])
    else:
        install_dir = detect_install_dir() or PF_ROOT / "PowerFactory 2024"
    cfg = Config(
        install_dir=install_dir,
        python_api=Path(pf.get("python_api") or install_dir / "Python" / PY_TAG),
        username=pf.get("username", ""),
        password=pf.get("password", ""),
        default_mode=pf.get("default_mode", "exclusive"),
        parallel_instance=pf.get("parallel_instance", "CLAUDE"),
        show_gui=bool(pf.get("show_gui", False)),
        extra_args=pf.get("extra_args", ""),
        source=path,
    )
    # Folders that are not set live under the workspace.
    ws = Path(paths.get("workspace") or DEFAULT_WORKSPACE)
    cfg.workspace = ws
    cfg.results_dir = Path(paths.get("results_dir") or ws / "results")
    cfg.backups_dir = Path(paths.get("backups_dir") or ws / "backups")
    cfg.logs_dir = Path(paths.get("logs_dir") or ws / "logs")
    cfg.allowed = [Path(p) for p in paths.get("allowed") or [ws]]
    for key in ("startup_s", "default_s", "inline_wait_s", "job_max_s", "idle_release_min"):
        if key in to:
            setattr(cfg, key, float(to[key]))
    if cfg.default_mode not in ("exclusive", "parallel"):
        raise ValueError(f"Invalid default_mode: {cfg.default_mode!r}")
    return cfg
