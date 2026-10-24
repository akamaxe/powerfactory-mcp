"""Additional studies: modal (ComMod), harmonics (ComHldf/ComFsweep), quasi-dynamic (ComStatsim),
P-V curves (own continuation with ComLdf.scLoadFac) and generic command execution."""

from __future__ import annotations

import datetime as dt
import math
import re

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import (
    find, get_attr, oname, resolve_many, rnd, study_command, table, temp_attrs, write_table,
)
from powerfactory_mcp.ops.results import column_values, find_result, load_columns, plot_columns, read_elmres


def _own_result(ctx, name: str):
    case = ctx.study_case()
    found = case.GetContents(f"{name}.ElmRes")
    return found[0] if found else case.CreateObject("ElmRes", name)


def _snapshot(case) -> dict:
    snap = {}
    for r in case.GetContents("*.ElmRes", 1):
        try:
            r.Load()
            snap[r.GetFullName()] = (r.GetNumberOfRows(), r.GetNumberOfColumns())
            r.Release()
        except Exception:  # noqa: BLE001
            snap[r.GetFullName()] = (None, None)
    return snap


# ------------------------------------------------------------------- modal
@op()
def run_modal_analysis(ctx, damping_threshold_pct: float = 5.0, participation: bool = True, max_modes: int = 60,
                       options: dict | None = None, export: str | bool | None = None) -> dict:
    mod = study_command(ctx, "ComMod")
    settings = {"iopt_met": 0, "iLeft": 1, "iRight": 1, "iPart": 1 if participation else 0}
    for k in ("isRecOscModesOnly", "isRecUnstabModesOnly", "isRecMinReal", "isRecMaxReal", "isRecMinImag",
              "isRecMaxImag", "isRecMinMagn", "isRecMaxMagn", "isRecMinDampRat", "isRecMaxDampRat"):
        if mod.HasAttribute(k):
            settings[k] = 0  # record all eigenvalues
    settings.update(options or {})
    with temp_attrs(ctx, mod, settings):
        ret = mod.Execute()
    if ret != 0:
        raise OpError(f"Modal analysis failed (ComMod={ret}). This is usually because the initial conditions "
                      "could not be calculated: check the messages.")
    res = get_attr(mod, "ResultFile")
    if res is None:
        cands = [r for r in ctx.study_case().GetContents("*.ElmRes", 1) if get_attr(r, "calTp") == 5]
        if not cands:
            raise OpError("Modal analysis ElmRes not found.")
        res = cands[-1]
    ctx.last_result = res
    cols = load_columns(ctx, res)
    idx = {c["variable"]: c["index"] for c in cols if c["is_meta"] or c["class"] == "ElmRes"}
    re_col, im_col = idx.get("b:eigvalr"), idx.get("b:eigvali")
    if re_col is None or im_col is None:
        raise OpError("The modal ElmRes has no b:eigvalr/b:eigvali.")
    sig, omg = column_values(ctx, res, re_col), column_values(ctx, res, im_col)
    part_cols = [c for c in cols if c["variable"].endswith("c:p_mag")]
    part_data = {c["index"]: column_values(ctx, res, c["index"]) for c in part_cols} if participation else {}
    modes = []
    for i, (s, w) in enumerate(zip(sig, omg)):
        if s is None or w is None or w < 0:  # conjugates: report the one with positive imaginary part
            continue
        f = w / (2 * math.pi)
        zeta = (-s / math.hypot(s, w) * 100) if (s or w) else None
        if s > 1e-6:
            cls = "UNSTABLE"
        elif w == 0:
            cls = "real (non-oscillatory)"
        elif zeta is not None and zeta < damping_threshold_pct:
            cls = "poorly damped"
        else:
            cls = "damped"
        band = ("" if w == 0 else "inter-area" if f < 0.7 else "local/plant" if f < 2.5 else "control")
        top = []
        if part_data:
            ranked = sorted(((part_data[c["index"]][i] or 0, c) for c in part_cols), key=lambda t: -t[0])[:3]
            top = [f"{c['object']}:{c['variable'].split(',')[0]} ({rnd(v, 3)})" for v, c in ranked if v > 1e-3]
        modes.append([i + 1, rnd(s, 5), rnd(w, 5), rnd(f, 4), rnd(zeta, 2), cls, band, top])
    order = {"UNSTABLE": 0, "poorly damped": 1, "damped": 2, "real (non-oscillatory)": 3}
    modes.sort(key=lambda m: (order.get(m[5], 9), m[4] if m[4] is not None else 999))
    colsout = ["mode", "real_1/s", "imag_rad/s", "freq_Hz", "damping_pct", "class", "band", "top_participation"]
    out = {
        "result_file": oname(res), "eigenvalues_total": len(sig),
        "unstable": sum(1 for m in modes if m[5] == "UNSTABLE"),
        "poorly_damped": sum(1 for m in modes if m[5] == "poorly damped"),
        "damping_threshold_pct": damping_threshold_pct,
        **table(colsout, modes, max_modes),
        "note": "Conjugate pairs reported once (imaginary part ≥ 0). Indicative bands: inter-area "
                "< 0.7 Hz; local/plant 0.7–2.5 Hz; control > 2.5 Hz.",
    }
    if export:
        p = ctx.out_path("modal", "csv", export if isinstance(export, str) else None)
        out["file"] = str(write_table(ctx, p, colsout, [m[:7] + ["; ".join(m[7])] for m in modes]))
    res.Release()
    return out


# --------------------------------------------------------------- harmonics
THD_CANDIDATES = ("m:THD", "m:THDu", "m:THF", "m:THDi")  # m:HD is the harmonic at fshow, not distortion


def _freq_check(ctx, cmd) -> dict:
    """Command frnom vs. the grid's nominal frequency (if they differ, the study is normalized wrongly)."""
    f_cmd = get_attr(cmd, "frnom")
    nets = ctx.app.GetProjectFolder("netdat").GetContents("*.ElmNet", 1)
    f_grid = get_attr(nets[0], "frnom") if nets else None
    d = {"frnom_command_Hz": f_cmd, "frnom_grid_Hz": f_grid}
    if f_cmd and f_grid and abs(f_cmd - f_grid) > 1e-6:
        d["warning"] = (f"The command uses frnom={f_cmd:g} Hz and the grid is {f_grid:g} Hz. Check the nominal "
                        "frequency in the project settings; you can force it with options={'frnom': ...}.")
    return d


@op()
def run_harmonic_analysis(ctx, analysis: str = "harmonic_load_flow", buses=None, f_start: float = 10.0,
                          f_stop: float = 2500.0, f_step: float = 5.0, options: dict | None = None,
                          plot: bool = True, export: str | bool | None = None) -> dict:
    targets = resolve_many(ctx, buses) if buses else [
        b for b in find(ctx, "*.ElmTerm", "calc", include_oos=False) if (get_attr(b, "iUsage") or 0) == 0]
    if analysis == "harmonic_load_flow":
        cmd = study_command(ctx, "ComHldf")
        res = _own_result(ctx, "MCP Harmonics")
        settings = {"iopt_thd": 1, "p_resvar": res, **(options or {})}
        with temp_attrs(ctx, cmd, settings):
            ret = cmd.Execute()
            if ret != 0:
                raise OpError(f"Harmonic load flow failed (ComHldf={ret}); see messages (are there harmonic sources?).")
            avail = [n for n in THD_CANDIDATES if targets and targets[0].HasAttribute(n)]
            rows = [[b.loc_name, rnd(get_attr(b, "uknom"), 3)] + [rnd(get_attr(b, n), 4) for n in avail]
                    for b in targets]
        cols = ["bus", "Un_kV"] + avail
        ii = 2 if avail else None
        if ii is not None:
            rows.sort(key=lambda r: -(r[ii] or 0))
        out = {"analysis": analysis, "frequency": _freq_check(ctx, cmd), **table(cols, rows, 200)}
        if not avail:
            out["note"] = ("No THD variables found on the buses; check the 'MCP Harmonics' ElmRes with "
                           "read_results or use run_python.")
        if export:
            p = ctx.out_path("harmonics", "csv", export if isinstance(export, str) else None)
            out["file"] = str(write_table(ctx, p, cols, rows))
        ctx.last_result = res
        return out
    if analysis == "frequency_sweep":
        cmd = study_command(ctx, "ComFsweep")
        res = _own_result(ctx, "MCP Sweep")
        for m in res.GetContents("*.IntMon"):
            m.Delete()
        for b in targets[:20]:
            for var in ("m:Z", "m:phiz"):
                res.AddVariable(b, var)
        settings = {"fstart": f_start, "fstop": f_stop, "fstep": f_step, "p_resvar": res, **(options or {})}
        with temp_attrs(ctx, cmd, settings):
            ret = cmd.Execute()
        if ret != 0:
            raise OpError(f"Frequency sweep failed (ComFsweep={ret}); see messages.")
        ctx.last_result = res
        fcheck = _freq_check(ctx, cmd)
        out = read_elmres(ctx, res, ["*:m:Z"], export=export, plot=plot, kind="frequency_sweep",
                          title="Impedance vs. harmonic order")
        out["resonances"] = _resonances(ctx, res, fcheck["frnom_command_Hz"] or 50.0)
        out["analysis"] = analysis
        out["frequency"] = fcheck
        out["note_x"] = "The ElmRes x axis (b:ifnow) is the harmonic order f/frnom; t_min/t_max are in that order."
        return out
    raise OpError("analysis: harmonic_load_flow | frequency_sweep")


def _resonances(ctx, res, frnom: float) -> dict:
    cols = load_columns(ctx, res)
    f = [h * frnom for h in (column_values(ctx, res, -1) or [])]  # harmonic order -> Hz
    out = {}
    for c in cols:
        if c["variable"] != "m:Z":
            continue
        z = column_values(ctx, res, c["index"]) or []
        peaks = [(f[i], z[i]) for i in range(1, len(z) - 1)
                 if z[i] is not None and z[i - 1] is not None and z[i + 1] is not None
                 and z[i] > z[i - 1] and z[i] > z[i + 1]]
        peaks.sort(key=lambda p: -p[1])
        out[c["object"]] = [[rnd(fp, 2), rnd(zp, 3)] for fp, zp in peaks[:3]]
    res.Release()
    return {"peaks_Hz_ohm": out, "note": "Local maxima of |Z|: candidate parallel resonances."}


# ------------------------------------------------------- quasi-dynamic
STEP_UNITS = {"seconds": 0, "minutes": 1, "hours": 2, "days": 3}
QDS_NET = {"ac": 0, "ac_unbalanced": 1, "dc": 2}


@op()
def run_quasi_dynamic(ctx, start: str | None = None, end: str | None = None, step: float = 1.0,
                      step_unit: str = "hours", method: str = "ac", variables: list[str] | None = None,
                      options: dict | None = None, plot: bool = True, export: bool = True) -> dict:
    qs = study_command(ctx, "ComStatsim")
    if float(step) != int(step) or step < 1:
        raise OpError("step must be an integer ≥ 1 (change step_unit for smaller steps).")
    settings = {"iopt_net": QDS_NET.get(method, 0), "stepSize": int(step), "stepUnit": STEP_UNITS.get(step_unit, 2)}
    if start or end:
        if not (start and end):
            raise OpError("Give both start and end (or neither to use the study case day).")
        t0 = int(dt.datetime.fromisoformat(start).timestamp())
        t1 = int(dt.datetime.fromisoformat(end).timestamp())
        if t1 <= t0:
            raise OpError("end must be later than start.")
        # 4 = user-defined period. End before start: PF validates start <= end on every change.
        settings.update(calcPeriod=4, endTime=t1, startTime=t0)
    settings.update(options or {})
    with temp_attrs(ctx, qs, settings):
        ret = qs.Execute()
    if ret != 0:
        raise OpError(f"Quasi-dynamic simulation failed (ComStatsim={ret}); see messages.")
    res = get_attr(qs, "results") or next(
        (r for r in ctx.study_case().GetContents("*.ElmRes", 1) if get_attr(r, "calTp") == 29), None)
    if res is None:
        raise OpError("Quasi-dynamic simulation ElmRes not found.")
    ctx.last_result = res
    out = read_elmres(ctx, res, variables, export=export, plot=plot, kind="quasi_dynamic",
                      title="Quasi-dynamic simulation")
    _unix_to_iso(out)
    out["note"] = ("The x axis is Unix time (PowerFactory local time): t_min/t_max were converted to dates. "
                   "Without load/generation profiles (ChaTime, ChaProfile) the results are constant.")
    return out


def _unix_to_iso(out: dict) -> None:
    st = out.get("stats")
    if not st:
        return
    for row in st["rows"]:
        for i in (2, 4):
            v = row[i]
            if isinstance(v, (int, float)) and v > 1e8:
                row[i] = dt.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------- P-V curves
@op()
def run_pv_curve(ctx, loads=None, buses=None, step_pct: float = 10.0, max_scale_pct: float = 400.0,
                 tolerance_pct: float = 0.5, scale_generation: bool = False, options: dict | None = None,
                 plot: bool = True, export: bool = True, cross_check: bool = True) -> dict:
    """Continuation by load scaling: increases the load until the load flow stops converging."""
    ldf = study_command(ctx, "ComLdf")
    mon = resolve_many(ctx, buses) if buses else [
        b for b in find(ctx, "*.ElmTerm", "calc", include_oos=False) if (get_attr(b, "iUsage") or 0) == 0]
    if not mon:
        raise OpError("No buses to record.")
    subset = resolve_many(ctx, loads) if loads else None
    base_scale = {lo.GetFullName(): get_attr(lo, "scale0") for lo in subset} if subset else {}

    def solve(lam: float) -> dict | None:
        settings = dict(options or {})
        if subset:
            for lo in subset:
                lo.SetAttribute("scale0", base_scale[lo.GetFullName()] * lam / 100.0)
        else:
            settings["scLoadFac"] = lam
            if scale_generation:
                settings["scGenFac"] = lam
        with temp_attrs(ctx, ldf, settings):
            ok = ldf.Execute() == 0 and ctx.app.IsLdfValid()
        if not ok:
            return None
        sg = ctx.app.GetSummaryGrid()
        return {"lam": lam, "P_load": get_attr(sg, "c:LoadP") if sg else None,
                "u": [get_attr(b, "m:u") for b in mon]}

    pts = []
    try:
        lam, last_ok, first_fail = 100.0, None, None
        while lam <= max_scale_pct:
            r = solve(lam)
            if r is None:
                first_fail = lam
                break
            pts.append(r)
            last_ok = lam
            lam += step_pct
        if last_ok is None:
            raise OpError("The base load flow does not converge: fix the case before the P-V curve.")
        if first_fail is not None:  # bisection of the collapse point
            lo_, hi_ = last_ok, first_fail
            for _ in range(20):
                if hi_ - lo_ <= tolerance_pct:
                    break
                mid = (lo_ + hi_) / 2
                r = solve(mid)
                if r is None:
                    hi_ = mid
                else:
                    pts.append(r)
                    lo_ = mid
            last_ok = lo_
    finally:
        if subset:
            for lo in subset:
                lo.SetAttribute("scale0", base_scale[lo.GetFullName()])
        ldf.Execute()  # leave the base case results
    pts.sort(key=lambda p: p["lam"])
    base, crit = pts[0], pts[-1]
    drops = [(base["u"][i] or 0) - (crit["u"][i] or 0) for i in range(len(mon))]
    order = sorted(range(len(mon)), key=lambda i: -drops[i])
    weakest = order[:6]
    out = {
        "collapse_reached": first_fail is not None,
        "base_load_MW": rnd(base["P_load"], 3), "max_load_MW": rnd(crit["P_load"], 3),
        "max_scale_pct": rnd(crit["lam"], 2), "loadability_margin_pct": rnd(crit["lam"] - 100.0, 2),
        "critical_bus": mon[order[0]].loc_name,
        "weakest_buses": table(["bus", "u_base_pu", "u_at_max_pu", "drop_pu"],
                               [[mon[i].loc_name, rnd(base["u"][i], 4), rnd(crit["u"][i], 4), rnd(drops[i], 4)]
                                for i in weakest]),
        "points": len(pts),
        "scaled": "selected loads (scale0)" if subset else "all loads (ComLdf.scLoadFac)"
                  + (" and generation" if scale_generation and not subset else ""),
    }
    if not out["collapse_reached"]:
        out["note"] = f"No collapse up to {max_scale_pct} %: raise max_scale_pct."
    header = ["scale_pct", "P_load_MW"] + [b.loc_name for b in mon]
    rows = [[rnd(p["lam"], 3), rnd(p["P_load"], 3)] + [rnd(u, 5) for u in p["u"]] for p in pts]
    if export:
        out["file"] = str(write_table(ctx, ctx.out_path("pv_curve", "csv"), header, rows))
    if plot:
        x = [p["P_load"] for p in pts]
        series = [({"object": mon[i].loc_name, "variable": "u", "unit": "p.u."}, [p["u"][i] for p in pts])
                  for i in weakest]
        out["plot"] = plot_columns(ctx, ctx.out_path("pv_curve", "png"), x, series,
                                   f"P-V curves – {ctx.brief()['study_case']}", "Total load [MW]")
    if cross_check:
        out["powerfactory_ComPvcurves"] = _pv_crosscheck(ctx)
    return out


def _pv_crosscheck(ctx) -> dict:
    try:
        pv = study_command(ctx, "ComPvcurves")
        ctx.ow.Clear()
        ret = pv.Execute()
        lines = [str(x) for x in ctx.ow.GetContent()]
        ctx.history.extend(lines)
        m = next((re.search(r"critical demand.*?:\s*([\d.,]+)\s*MW", x) for x in lines
                  if "critical demand" in x.lower()), None)
        d = {"return_code": ret}
        if m:
            d["critical_demand_MW"] = float(m.group(1).replace(".", "").replace(",", "."))
        ctx.app.GetFromStudyCase("ComLdf").Execute()
        return d
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ------------------------------------------------------------ generic command
@op()
def run_command(ctx, class_name: str, settings: dict | None = None, result: str | None = None,
                variables: list[str] | None = None, plot: bool = False, export: bool = False) -> dict:
    if not re.fullmatch(r"Com[A-Za-z0-9]+", class_name):
        raise OpError("class_name must be a command (ComXxx).")
    case = ctx.study_case()
    cmd = study_command(ctx, class_name)
    before = _snapshot(case)
    with temp_attrs(ctx, cmd, settings):
        ret = cmd.Execute()
    after = _snapshot(case)
    changed = [k.split("\\")[-1] for k, v in after.items() if before.get(k) != v]
    out = {"command": oname(cmd), "return_code": ret, "ok": ret == 0, "changed_results": changed}
    if result:
        res = find_result(ctx, result)
        ctx.last_result = res
        out.update(read_elmres(ctx, res, variables, export=export, plot=plot, kind=class_name))
    return out
