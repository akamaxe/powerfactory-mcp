"""Projects: list, activate, overview, study cases/scenarios/variations, import and export .pfd."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import get_attr, oname, rnd, safe_name

GEN_CLASSES = ("ElmSym", "ElmGenstat", "ElmPvsys", "ElmAsm")


def _walk_projects(folder, base: str, out: list, depth: int = 0) -> None:
    """Walks folders (without entering projects: a recursive GetContents on the user is extremely slow)."""
    if depth > 12:
        return
    for child in folder.GetContents():
        cls = child.GetClassName()
        if cls == "IntPrj":
            full = child.GetFullName()
            out.append((child, full[len(base):].lstrip("\\") if full.startswith(base) else full))
        elif cls == "IntFolder":
            _walk_projects(child, base, out, depth + 1)


def quiet_activate(ctx, obj) -> int:
    """Activates a project or study case. If the GUI has already been initialized (after exporting a diagram),
    PowerFactory opens and redraws the case desktop: with the SEIN it goes from about 25 s to over 10 min.
    With graphic updates disabled it drops to about 90 s, and the desktop is closed afterwards."""
    if not getattr(ctx, "gui_initialized", False):
        return obj.Activate()
    from powerfactory_mcp.ops.graphics import close_graphics
    app = ctx.app
    app.SetGuiUpdateEnabled(0)
    app.SetGraphicUpdate(0)
    try:
        return obj.Activate()
    finally:
        app.SetGraphicUpdate(1)
        app.SetGuiUpdateEnabled(1)
        if not ctx.gui_visible:
            close_graphics(ctx)


def all_projects(ctx) -> list:
    user = ctx.user()
    out: list = []
    _walk_projects(user, user.GetFullName(), out)
    return out


@op()
def list_projects(ctx, contains: str | None = None) -> dict:
    active = ctx.app.GetActiveProject()
    act_path = active.GetFullName() if active else None
    rows = []
    for prj, path in all_projects(ctx):
        if contains and contains.lower() not in path.lower():
            continue
        rows.append({"name": prj.loc_name, "path": path, "active": prj.GetFullName() == act_path})
    return {"user": ctx.user().loc_name, "count": len(rows), "projects": rows}


def find_project(ctx, spec: str):
    spec = spec.strip()
    if spec.startswith("\\"):
        o = ctx.user().SearchObject(spec if spec.endswith(".IntPrj") else spec + ".IntPrj")
        if o is None:
            raise OpError(f"Project {spec} does not exist")
        return o
    projects = all_projects(ctx)
    key = spec.removesuffix(".IntPrj").lower()
    for test in (
        lambda p, path: path.lower().removesuffix(".intprj") == key,           # exact relative path
        lambda p, path: p.loc_name.lower() == key,                              # exact name
        lambda p, path: key in path.lower(),                                    # substring
    ):
        hits = [(p, path) for p, path in projects if test(p, path)]
        if len(hits) == 1:
            return hits[0][0]
        if len(hits) > 1:
            listing = "; ".join(path for _, path in hits[:10])
            raise OpError(f"'{spec}' matches {len(hits)} projects: {listing}. "
                          "Use the relative path (as given by list_projects).")
    raise OpError(f"Project '{spec}' not found. Use list_projects().")


@op()
def open_project(ctx, project: str, study_case: str | None = None) -> dict:
    prj = find_project(ctx, project)
    active = ctx.app.GetActiveProject()
    already = active is not None and active.GetFullName() == prj.GetFullName()
    if not already and quiet_activate(ctx, prj) != 0:
        raise OpError(f"PowerFactory could not activate {prj.GetFullName()} (see messages).")
    if study_case:
        _activate_case(ctx, study_case)
    return project_overview(ctx)


def _activate_case(ctx, name: str):
    study = ctx.app.GetProjectFolder("study")
    cases = study.GetContents("*.IntCase", 1) if study else []
    hits = [c for c in cases if c.loc_name == name] or [c for c in cases if name.lower() in c.loc_name.lower()]
    if len(hits) != 1:
        names = ", ".join(c.loc_name for c in cases)
        raise OpError(f"Study case '{name}' {'ambiguous' if hits else 'not found'}. Cases: {names}")
    active = ctx.app.GetActiveStudyCase()
    if active is not None and active.GetFullName() == hits[0].GetFullName():
        return hits[0]  # Activate() returns 1 if the case is already active
    if quiet_activate(ctx, hits[0]) != 0:
        raise OpError(f"Could not activate study case {hits[0].loc_name}.")
    return hits[0]


def _folder_items(ctx, key: str, pattern: str) -> list:
    f = ctx.app.GetProjectFolder(key)
    return f.GetContents(pattern, 1) if f else []


@op()
def project_overview(ctx) -> dict:
    app = ctx.app
    prj = ctx.project()
    case = app.GetActiveStudyCase()
    scen = app.GetActiveScenario()
    d: dict = {
        "project": prj.loc_name,
        "path": prj.GetFullName()[len(ctx.user().GetFullName()):].lstrip("\\"),
        "study_case": case.loc_name if case else None,
        "study_cases": [c.loc_name for c in _folder_items(ctx, "study", "*.IntCase")],
        "scenario": scen.loc_name if scen else None,
        "scenarios": [s.loc_name for s in _folder_items(ctx, "scen", "*.IntScenario")],
        "variations": [{"name": v.loc_name, "active": bool(get_attr(v, "e:iActive", 0) or 0)}
                       for v in _folder_items(ctx, "scheme", "*.IntScheme")],
        "grids": [{"name": g.loc_name, "f_nom_Hz": get_attr(g, "frnom")}
                  for g in _folder_items(ctx, "netdat", "*.ElmNet")],
        "diagrams": [g.loc_name for g in _folder_items(ctx, "dia", "*.IntGrfnet")],
    }
    if case is None:
        d["note"] = "No active study case: use set_active(study_case=...)."
        return d
    objs = app.GetCalcRelevantObjects("*.Elm*,*.StaSwitch,*.Rel*")
    counts = Counter(o.GetClassName() for o in objs)
    d["elements"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    terms = [o for o in objs if o.GetClassName() == "ElmTerm"]
    levels = Counter(rnd(get_attr(t, "uknom", 0.0), 3) for t in terms)
    d["voltage_levels_kV"] = {str(k): v for k, v in sorted(levels.items(), key=lambda kv: -(kv[0] or 0))}
    gens = [o for o in objs if o.GetClassName() in GEN_CLASSES and not o.IsOutOfService()]
    loads = [o for o in objs if o.GetClassName().startswith("ElmLod") and not o.IsOutOfService()]
    d["totals"] = {
        "generators_in_service": len(gens),
        "gen_dispatch_MW": rnd(sum((get_attr(g, "pgini", 0.0) or 0.0) * (get_attr(g, "ngnum", 1) or 1)
                                   for g in gens if g.GetClassName() != "ElmAsm"), 2),
        "gen_rating_MVA": rnd(sum((get_attr(g, "sgn", 0.0) or 0.0) * (get_attr(g, "ngnum", 1) or 1)
                                  for g in gens if g.GetClassName() != "ElmAsm"), 2),
        "loads_in_service": len(loads),
        "load_P_MW": rnd(sum(get_attr(lo, "plini", 0.0) or 0.0 for lo in loads), 2),
        "load_Q_Mvar": rnd(sum(get_attr(lo, "qlini", 0.0) or 0.0 for lo in loads), 2),
        "external_grids": sum(1 for o in objs if o.GetClassName() == "ElmXnet"),
    }
    d["valid_results"] = {"load_flow": bool(app.IsLdfValid()), "short_circuit": bool(app.IsShcValid())}
    return d


@op()
def set_active(ctx, study_case: str | None = None, scenario: str | None = None,
               variations: list[str] | None = None, deactivate_scenario: bool = False) -> dict:
    ctx.project()
    done = {}
    if study_case:
        done["study_case"] = _activate_case(ctx, study_case).loc_name
    if deactivate_scenario:
        s = ctx.app.GetActiveScenario()
        if s is not None:
            s.Deactivate()
        done["scenario"] = None
    elif scenario:
        hits = [s for s in _folder_items(ctx, "scen", "*.IntScenario") if s.loc_name == scenario]
        if not hits:
            raise OpError(f"Scenario '{scenario}' not found.")
        if hits[0].Activate() != 0:
            raise OpError(f"Could not activate scenario {scenario}.")
        done["scenario"] = scenario
    if variations is not None:
        allv = _folder_items(ctx, "scheme", "*.IntScheme")
        want = set(variations)
        for v in allv:
            if v.loc_name in want:
                v.Activate()
            elif get_attr(v, "e:iActive", 0):
                v.Deactivate()
        done["variations"] = sorted(want & {v.loc_name for v in allv})
    return {"applied": done, **ctx.brief()}


@op()
def import_project(ctx, path: str, target_folder: str | None = None, activate: bool = False) -> dict:
    src = ctx.cfg.check_path(path, must_exist=True)
    if src.suffix.lower() not in (".pfd", ".dz"):
        raise OpError("Only .pfd or .dz files can be imported")
    user = ctx.user()
    target = user
    if target_folder:
        target = user.SearchObject(user.GetFullName() + "\\" + target_folder.strip("\\") + ".IntFolder")
        if target is None:
            target = user
            for part in target_folder.strip("\\").split("\\"):
                nxt = target.SearchObject(target.GetFullName() + "\\" + part + ".IntFolder")
                target = nxt if nxt is not None else target.CreateObject("IntFolder", part)
    before = {p.GetFullName() for p, _ in all_projects(ctx)}
    if src.suffix.lower() == ".dz":
        objs = ctx.app.ImportDz(target, str(src))
        imported = [o for o in objs if o.GetClassName() == "IntPrj"]
    else:
        imp = user.CreateObject("ComPfdimport", "pfmcp_import")
        try:
            imp.SetAttribute("g_file", str(src))
            imp.SetAttribute("g_target", target)
            ret = imp.Execute()
            if ret:
                raise OpError(f"Import failed (code {ret}); see messages.")
            imported = [o for o in imp.GetImportedObjects() if o.GetClassName() == "IntPrj"]
        finally:
            imp.Delete()
    if not imported:
        imported = [p for p, _ in all_projects(ctx) if p.GetFullName() not in before]
    res = {"imported": [p.GetFullName()[len(user.GetFullName()):].lstrip("\\") for p in imported],
           "target": target.GetFullName()}
    if activate and imported:
        imported[0].Activate()
        res["overview"] = project_overview(ctx)
    return res


@op()
def export_project(ctx, project: str | None = None, path: str | None = None) -> dict:
    prj = find_project(ctx, project) if project else ctx.project()
    if path:
        dst = ctx.cfg.check_path(path)
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = Path(ctx.cfg.backups_dir) / f"{safe_name(prj.loc_name)}_{stamp}.pfd"
    dst.parent.mkdir(parents=True, exist_ok=True)
    user = ctx.user()
    exp = user.CreateObject("CompfdExport", "pfmcp_export")
    if exp is None:
        raise OpError("Could not create the CompfdExport export command.")
    try:
        exp.SetAttribute("g_objects", [prj])
        exp.SetAttribute("g_file", str(dst))
        ret = exp.Execute()
    finally:
        exp.Delete()
    if ret or not dst.exists():
        raise OpError(f"Export failed (code {ret}); see messages.")
    return {"project": oname(prj), "file": str(dst), "size_MB": round(dst.stat().st_size / 2**20, 2)}
