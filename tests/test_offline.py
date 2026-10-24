"""Tests without PowerFactory: configuration, paths, utilities and tool registration."""

import json
from pathlib import Path

import pytest

from powerfactory_mcp.config import PathNotAllowed, load_config
from powerfactory_mcp.ops import common


@pytest.fixture()
def cfg(tmp_path):
    toml = tmp_path / "c.toml"
    allowed = tmp_path / "ok"
    allowed.mkdir()
    toml.write_text(
        "[powerfactory]\ndefault_mode = 'parallel'\n"
        f"[paths]\nworkspace = '{allowed}'\nallowed = ['{allowed}']\n"
        "[timeouts]\ndefault_s = 12\nidle_release_min = 0\n",
        encoding="utf-8",
    )
    return load_config(toml), allowed


def test_load_config(cfg):
    c, allowed = cfg
    assert c.default_mode == "parallel"
    assert c.default_s == 12
    assert c.idle_release_min == 0
    assert c.allowed == [allowed]
    assert c.to_dict()["password"] == ""


def test_check_path(cfg, tmp_path):
    c, allowed = cfg
    assert c.check_path(allowed / "a.csv") == allowed / "a.csv"
    assert c.check_path("rel.csv") == allowed / "rel.csv"  # relative to the workspace
    with pytest.raises(PathNotAllowed):
        c.check_path(tmp_path / "outside.csv")
    with pytest.raises(PathNotAllowed):
        c.check_path(allowed / ".." / "escape.csv")


def test_invalid_mode(tmp_path):
    toml = tmp_path / "c.toml"
    toml.write_text("[powerfactory]\ndefault_mode = 'bogus'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(toml)


def test_with_class():
    assert common.with_class("Bus 1") == "Bus 1.*"
    assert common.with_class("Bus 1.ElmTerm") == "Bus 1.ElmTerm"
    assert common.with_class("*.ElmLne") == "*.ElmLne"
    assert common.with_class("Line 1.2") == "Line 1.2.*"  # '.2' is not a class


def test_table_and_jsonable():
    t = common.table(["a"], [[1], [2], [3]], limit=2)
    assert t["total"] == 3 and t["truncated"] and len(t["rows"]) == 2
    assert common.jsonable({"x": float("nan"), "y": (1, 2), 3: "z"}) == {"x": None, "y": [1, 2], "3": "z"}
    assert common.rnd(1.234567, 2) == 1.23


def test_tcc_curves():
    from powerfactory_mcp.ops.protection import classify_curve, stage_curve

    assert classify_curve("IEC Class B (Very Inverse)") == "iec_very"
    assert classify_curve("IEC Normal Inverse") == "iec_standard"
    assert classify_curve("Definite Time") == "definite"
    assert classify_curve("IEEE Extremely Inverse") == "ieee_extremely"
    assert classify_curve("Odd curve") is None
    # Case verified in PowerFactory: IEC very inverse I>, Ipset 1.1 A sec, TMS 1.0, I = 2.2126 A sec -> 13.347 s
    t = stage_curve("iec_very", 1.1, 1.0, [2.2126])[0]
    assert abs(t - 13.347) < 0.05
    assert stage_curve("definite", 4.0, 0.2, [3.9, 5.0]) == [None, 0.2]


def test_server_tools_registered():
    import asyncio
    from powerfactory_mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {"status", "connect", "disconnect", "list_projects", "open_project", "run_load_flow",
                "run_short_circuit", "run_contingency_analysis", "run_dynamic_simulation", "protection_study",
                "run_modal_analysis", "run_pv_curve", "read_results", "export_diagram", "run_python",
                "describe_class", "set_attributes", "add_element", "delete_objects", "jobs"}
    assert expected <= names, expected - names
    from powerfactory_mcp import ops
    ops.load_all()
    # Every tool that delegates to the worker has its own operation.
    server_only = {"status", "connect", "disconnect", "jobs"}
    assert (names - server_only) <= set(ops.OPS), (names - server_only) - set(ops.OPS)


def test_state_without_worker():
    from powerfactory_mcp.server import _client

    st = _client.state()
    assert st["connected"] is False
    json.dumps(st)


def test_derived_paths_and_detection(tmp_path, monkeypatch):
    from powerfactory_mcp import config

    root = tmp_path / "DIgSILENT"
    for name, tags in (("PowerFactory 2022", ["3.10"]), ("PowerFactory 2024", ["3.11", config.PY_TAG])):
        for tag in tags:
            d = root / name / "Python" / tag
            d.mkdir(parents=True)
            (d / "powerfactory.pyd").write_bytes(b"")
    assert config.detect_install_dir(root=root) == root / "PowerFactory 2024"
    assert config.detect_install_dir("3.10", root=root) == root / "PowerFactory 2022"
    assert config.detect_install_dir("2.7", root=root) is None

    monkeypatch.setattr(config, "detect_install_dir", lambda: root / "PowerFactory 2024")
    ws = tmp_path / "ws"
    toml = tmp_path / "c.toml"
    toml.write_text(f"[powerfactory]\ninstall_dir = ''\n[paths]\nworkspace = '{ws}'\n", encoding="utf-8")
    c = config.load_config(toml)
    assert c.install_dir == root / "PowerFactory 2024"
    assert c.python_api == root / "PowerFactory 2024" / "Python" / config.PY_TAG
    assert (c.results_dir, c.backups_dir, c.logs_dir) == (ws / "results", ws / "backups", ws / "logs")
    assert c.allowed == [ws]


def test_installer_writes_valid_config(tmp_path, monkeypatch):
    import importlib.util

    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("pf_install", repo / "install.py")
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    monkeypatch.setattr(inst, "say", lambda *a, **k: None)
    ws, pf = tmp_path / "My O'Workspace", Path(r"C:\Program Files\DIgSILENT\PowerFactory 2024")
    cfg_path = inst.write_config(ws, pf)
    c = load_config(cfg_path)
    assert c.install_dir == pf and c.python_api == pf / "Python" / inst.PY_TAG
    assert c.workspace == ws and c.logs_dir == ws / "logs"
    assert ws in c.allowed or any(ws.is_relative_to(p) for p in c.allowed)
    assert all((ws / d).is_dir() for d in inst.SUBDIRS)
    assert inst.write_config(ws, pf) == cfg_path  # an existing TOML is left untouched
    # The repository template is also valid TOML as is.
    assert load_config(repo / "powerfactory-mcp.example.toml").default_mode == "exclusive"


def test_log_append_across_processes(tmp_path):
    """Several servers and their workers write to the same worker.log without clobbering each other."""
    import subprocess
    import sys

    from powerfactory_mcp.client import open_append

    log = tmp_path / "worker.log"
    code = "import sys\nfor i in range(400): print(sys.argv[1], i, 'x' * 60, flush=True)"
    procs = []
    for tag in ("A", "B", "C"):  # one handle per "server", inherited by its "worker"
        with open_append(log) as f:
            f.write(f"== {tag}\n")
            f.flush()
            procs.append(subprocess.Popen([sys.executable, "-c", code, tag], stdout=f))
    for p in procs:
        p.wait(60)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 + 3 * 400
    assert all(line.startswith("== ") or len(line.split()) == 3 for line in lines)
