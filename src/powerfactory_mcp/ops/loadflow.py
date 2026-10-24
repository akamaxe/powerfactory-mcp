"""Load flow (ComLdf) and N-1 contingency analysis (ComSimoutage)."""

from __future__ import annotations

import math

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import (
    find, get_attr, grid_of, oname, resolve_many, rnd, study_command, table, temp_attrs, write_table,
)

NET_MODES = {"ac": 0, "balanced": 0, "unbalanced": 1, "ac_unbalanced": 1, "dc": 2}
BRANCH_CLASSES = "*.ElmLne,*.ElmTr2,*.ElmTr3,*.ElmZpu,*.ElmSind,*.ElmScap,*.ElmTr4"
GEN_CLASSES = "*.ElmSym,*.ElmGenstat,*.ElmPvsys,*.ElmXnet,*.ElmAsm"
SIDE = {"ElmTr2": "bushv", "ElmTr3": "bushv", "ElmTr4": "bushv"}
NOT_CONVERGED_HINTS = [
    "Check the reference machine (slack): ElmXnet with bustp='SL' or ElmSym with ip_ctrl=1 in each energized island.",
    "Look for islands without a reference and isolated elements (messages 'separated area' / 'de-energised').",
    "Try options={'iopt_lev': 1} (automatic model adaptation for convergence).",
    "Try more iterations: options={'itrlx': 50, 'ictrlx': 30}, or iopt_lim=0 to ignore Q limits.",
    "Check for absurd data: zero impedances, nominal voltages inconsistent between bus and type, huge loads.",
]


def _num(v):
    return v if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)) else None


def bus_rows(ctx, unbalanced: bool) -> tuple[list[str], list[list]]:
    cols = ["bus", "grid", "Un_kV", "u_pu", "U_kV", "angle_deg"]
    if unbalanced:
        cols += ["uA_pu", "uB_pu", "uC_pu"]
    rows = []
    for t in find(ctx, "*.ElmTerm", "calc", include_oos=False):
        u = _num(get_attr(t, "m:u"))
        if u is None:
            continue
        row = [t.loc_name, grid_of(t), rnd(get_attr(t, "uknom"), 3), rnd(u, 4), rnd(get_attr(t, "m:Ul"), 3),
               rnd(get_attr(t, "m:phiu"), 2)]
        if unbalanced:
            row += [rnd(get_attr(t, f"m:u:{ph}"), 4) for ph in "ABC"]
        rows.append(row)
    return cols, rows


def branch_rows(ctx) -> tuple[list[str], list[list]]:
    cols = ["branch", "class", "loading_pct", "P_MW", "Q_Mvar", "I_kA", "Ploss_MW"]
    rows = []
    for b in find(ctx, BRANCH_CLASSES, "calc", include_oos=False):
        side = SIDE.get(b.GetClassName(), "bus1")
        ld = _num(get_attr(b, "c:loading"))
        if ld is None:
            continue
        rows.append([b.loc_name, b.GetClassName(), rnd(ld, 2), rnd(get_attr(b, f"m:P:{side}"), 3),
                     rnd(get_attr(b, f"m:Q:{side}"), 3), rnd(get_attr(b, f"m:I:{side}"), 4),
                     rnd(get_attr(b, "c:Ploss"), 4)])
    return cols, rows


def gen_rows(ctx) -> tuple[list[str], list[list]]:
    cols = ["generator", "class", "P_MW", "Q_Mvar", "u_pu"]
    rows = []
    for g in find(ctx, GEN_CLASSES, "calc", include_oos=False):
        p = _num(get_attr(g, "m:P:bus1"))
        if p is None:
            continue
        rows.append([g.loc_name, g.GetClassName(), rnd(p, 3), rnd(get_attr(g, "m:Q:bus1"), 3),
                     rnd(get_attr(g, "m:u1:bus1"), 4)])
    return cols, rows


def grid_summary(ctx) -> dict:
    sg = ctx.app.GetSummaryGrid()
    if sg is None:
        return {}
    keys = {"gen_P_MW": "c:GenP", "gen_Q_Mvar": "c:GenQ", "load_P_MW": "c:LoadP", "load_Q_Mvar": "c:LoadQ",
            "loss_P_MW": "c:LossP", "loss_Q_Mvar": "c:LossQ"}
    return {k: rnd(get_attr(sg, v), 3) for k, v in keys.items()}


@op()
def run_load_flow(ctx, method: str = "ac", options: dict | None = None, v_min: float = 0.95,
                  v_max: float = 1.05, loading_max: float = 100.0, report: str = "violations",
                  limit: int = 200, export: str | bool | None = None) -> dict:
    """report: summary | violations | full."""
    m = method.lower()
    if m not in NET_MODES:
        raise OpError(f"Invalid method: {method} (ac | unbalanced | dc)")
    ldf = study_command(ctx, "ComLdf")
    settings = {"iopt_net": NET_MODES[m], **(options or {})}
    with temp_attrs(ctx, ldf, settings):
        ret = ldf.Execute()
    out: dict = {"converged": ret == 0, "return_code": ret, "method": m}
    if ret != 0 or not ctx.app.IsLdfValid():
        out["converged"] = False
        out["hints"] = NOT_CONVERGED_HINTS
        return out
    out["summary"] = grid_summary(ctx)
    bcols, brows = bus_rows(ctx, m == "unbalanced")
    rcols, rrows = branch_rows(ctx)
    gcols, grows = gen_rows(ctx)
    iu = bcols.index("u_pu")
    il = rcols.index("loading_pct")
    under = sorted([r for r in brows if r[iu] is not None and r[iu] < v_min], key=lambda r: r[iu])
    over = sorted([r for r in brows if r[iu] is not None and r[iu] > v_max], key=lambda r: -r[iu])
    overload = sorted([r for r in rrows if r[il] is not None and r[il] > loading_max], key=lambda r: -r[il])
    volts = [r[iu] for r in brows if r[iu] is not None]
    out["limits"] = {"v_min_pu": v_min, "v_max_pu": v_max, "loading_max_pct": loading_max}
    out["violations"] = {"undervoltage": len(under), "overvoltage": len(over), "overloaded": len(overload)}
    if volts:
        vmin_row = min(brows, key=lambda r: r[iu] if r[iu] is not None else 9)
        vmax_row = max(brows, key=lambda r: r[iu] if r[iu] is not None else -9)
        out["extremes"] = {"u_min": [vmin_row[0], vmin_row[iu]], "u_max": [vmax_row[0], vmax_row[iu]]}
    out["top_loaded"] = table(rcols, sorted(rrows, key=lambda r: -(r[il] or 0))[:5])
    if report in ("violations", "full"):
        out["undervoltage"] = table(bcols, under, limit)
        out["overvoltage"] = table(bcols, over, limit)
        out["overloaded"] = table(rcols, overload, limit)
    if report == "full":
        out["buses"] = table(bcols, brows, limit)
        out["branches"] = table(rcols, rrows, limit)
        out["generators"] = table(gcols, grows, limit)
    if export:
        path = ctx.out_path("load_flow", "xlsx", export if isinstance(export, str) else None)
        if path.suffix.lower() == ".xlsx":
            import pandas as pd
            with pd.ExcelWriter(path) as xw:
                pd.DataFrame(brows, columns=bcols).to_excel(xw, sheet_name="buses", index=False)
                pd.DataFrame(rrows, columns=rcols).to_excel(xw, sheet_name="branches", index=False)
                pd.DataFrame(grows, columns=gcols).to_excel(xw, sheet_name="generators", index=False)
            out["file"] = str(path)
        else:
            out["files"] = [str(write_table(ctx, path.with_name(path.stem + suf + path.suffix), c, r))
                            for suf, c, r in (("_buses", bcols, brows), ("_branches", rcols, rrows),
                                              ("_generators", gcols, grows))]
    return out


# ------------------------------------------------------------- contingencies
@op()
def run_contingency_analysis(ctx, elements: str | list = "*.ElmLne,*.ElmTr2,*.ElmTr3", method: str = "ac",
                             v_min: float = 0.95, v_max: float = 1.05, loading_max: float = 100.0,
                             top_n: int = 20, export: str | bool | None = None) -> dict:
    """N-1 with an own ComSimoutage ('MCP N-1') in the active case: it does not touch your commands or fault cases."""
    case = ctx.study_case()
    elms = [e for e in resolve_many(ctx, elements) if not e.IsOutOfService()]
    if not elms:
        raise OpError("No in-service elements for the contingencies.")
    old = case.GetContents("MCP N-1.ComSimoutage")
    for o in old:
        o.Delete()
    cont = case.CreateObject("ComSimoutage", "MCP N-1")
    cont.SetAttribute("iopt_Linear", 1 if method.lower() == "dc" else 0)
    for e in elms:
        co = cont.CreateObject("ComOutage", e.loc_name[:40])
        co.SetObjs([e])
    cont.SetLimits(v_min, v_max, loading_max)
    ret = cont.Execute()
    if ret != 0:
        raise OpError(f"Contingency analysis failed (code {ret}); see messages.")
    res = cont.GetAttribute("p_rescnt") or next(iter(case.GetContents("Contingency Analysis AC.ElmRes")), None)
    if res is None:
        raise OpError("Contingency ElmRes not found.")
    data = _read_contingency(ctx, res, cont)
    worst_load = sorted(data["branch_max"].items(), key=lambda kv: -kv[1][0])
    worst_low = sorted(data["bus_min"].items(), key=lambda kv: kv[1][0])
    worst_high = sorted(data["bus_max"].items(), key=lambda kv: -kv[1][0])
    lcols = ["branch", "max_loading_pct", "contingency"]
    vcols = ["bus", "u_pu", "contingency"]
    lrows = [[k, rnd(v[0], 2), v[1]] for k, v in worst_load]
    out = {
        "contingencies": len(elms), "method": method, "result_file": oname(res),
        "limits": {"v_min_pu": v_min, "v_max_pu": v_max, "loading_max_pct": loading_max},
        "non_converged": data["non_converged"],
        "overloads": table(lcols, [r for r in lrows if r[1] > loading_max], top_n),
        "most_loaded": table(lcols, lrows, top_n),
        "undervoltage": table(vcols, [[k, rnd(v[0], 4), v[1]] for k, v in worst_low if v[0] < v_min], top_n),
        "overvoltage": table(vcols, [[k, rnd(v[0], 4), v[1]] for k, v in worst_high if v[0] > v_max], top_n),
        "per_contingency": table(["contingency", "max_loading_pct", "worst_branch", "u_min_pu", "worst_bus",
                                  "converged"],
                                 sorted(data["per_case"], key=lambda r: -(r[1] or 0)), top_n),
        "note": "Values are post-contingency maxima/minima recorded by PowerFactory (values outside the recording "
                "limits are not stored). The 'MCP N-1' command is left in the case for inspection.",
    }
    if export:
        path = ctx.out_path("n-1", "xlsx", export if isinstance(export, str) else None)
        import pandas as pd
        with pd.ExcelWriter(path) as xw:
            pd.DataFrame(lrows, columns=lcols).to_excel(xw, sheet_name="branches", index=False)
            pd.DataFrame([[k, v[0], v[1]] for k, v in worst_low], columns=vcols).to_excel(
                xw, sheet_name="voltage_min", index=False)
            pd.DataFrame(data["per_case"], columns=out["per_contingency"]["columns"]).to_excel(
                xw, sheet_name="contingencies", index=False)
        out["file"] = str(path)
    return out


def _read_contingency(ctx, res, cont) -> dict:
    """Walks the contingency ElmRes (calTp=13): rows = cases, columns = variables per element."""
    from powerfactory_mcp.ops.results import column_values, load_columns

    cols = load_columns(ctx, res)
    nrows = res.GetNumberOfRows()
    names = {}
    outages = cont.GetContents("*.ComOutage")
    numbers = column_values(ctx, res, _col(cols, None, "b:number"))
    nonconv = column_values(ctx, res, _col(cols, None, "b:inoconv"))
    for co in outages:
        names[int(get_attr(co, "number", -1) or -1)] = co.loc_name
    case_name = []
    for r in range(nrows):
        n = numbers[r] if numbers else None
        case_name.append("base" if n is not None and n < 0 else names.get(int(n) if n is not None and n < 1e30
                                                                           else -99, f"case {r}"))
    branch_max: dict = {}
    bus_min: dict = {}
    bus_max: dict = {}
    per_case = [[case_name[r], None, None, None, None, not (nonconv and nonconv[r] and nonconv[r] < 1e30
                                                            and nonconv[r] > 0)] for r in range(nrows)]
    for c in cols:
        obj, var = c["object"], c["variable"]
        if obj is None or c["is_meta"]:
            continue
        if var == "c:loading":
            vals = column_values(ctx, res, c["index"])
            for r, v in enumerate(vals):
                if v is None or v >= 1e30 or case_name[r] == "base":
                    continue
                if v > branch_max.get(obj, (-1, None))[0]:
                    branch_max[obj] = (v, case_name[r])
                if v > (per_case[r][1] or -1):
                    per_case[r][1], per_case[r][2] = rnd(v, 2), obj
        elif var in ("m:u", "m:u1"):
            vals = column_values(ctx, res, c["index"])
            for r, v in enumerate(vals):
                if v is None or v >= 1e30 or case_name[r] == "base" or v <= 0:
                    continue
                if v < bus_min.get(obj, (9, None))[0]:
                    bus_min[obj] = (v, case_name[r])
                if v > bus_max.get(obj, (-9, None))[0]:
                    bus_max[obj] = (v, case_name[r])
                if per_case[r][3] is None or v < per_case[r][3]:
                    per_case[r][3], per_case[r][4] = rnd(v, 4), obj
    per_case = [row for row in per_case if row[0] != "base"]
    return {"branch_max": branch_max, "bus_min": bus_min, "bus_max": bus_max, "per_case": per_case,
            "non_converged": [row[0] for row in per_case if not row[5]]}


def _col(cols, obj, var):
    for c in cols:
        if c["variable"] == var and (obj is None or c["object"] == obj):
            return c["index"]
    return None
