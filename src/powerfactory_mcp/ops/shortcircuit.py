"""Short circuit (ComShc): IEC 60909, VDE, ANSI, complete and DC standards."""

from __future__ import annotations

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import (
    find, get_attr, grid_of, oname, resolve_many, rnd, study_command, table, temp_attrs, write_table,
)

# Verified in PF 2024 (ComShc.iopt_mde).
METHODS = {"vde": 0, "vde0102": 0, "iec": 1, "iec60909": 1, "ansi": 2, "complete": 3,
           "iec61363": 4, "iec61660": 5, "dc_iec61660": 5, "ieee946": 6, "dc_ieee946": 6, "vde_dc": 7}
# ComShc.iopt_shc: an invalid value is stored without error and computed as three-phase, so it is validated here.
FAULTS = {"3ph": "3psc", "3psc": "3psc", "2ph": "2psc", "2psc": "2psc", "ll": "2psc",
          "1ph": "spgf", "slg": "spgf", "spgf": "spgf", "lg": "spgf",
          "2phg": "2pgf", "llg": "2pgf", "2pgf": "2pgf", "3ph_unbalanced": "3rst", "3rst": "3rst"}
ALLBUS = {"busbars": 2, "all": 2, "all_nodes": 1}


@op()
def run_short_circuit(ctx, method: str = "iec60909", fault_type: str = "3ph", locations: str | list = "busbars",
                      current: str = "max", rf_ohm: float | None = None, xf_ohm: float | None = None,
                      options: dict | None = None, branch_contributions: bool = False, limit: int = 300,
                      export: str | bool | None = None) -> dict:
    """locations: 'busbars' (all busbars), 'all_nodes' (busbars and junction nodes) or a list of buses."""
    mk, fk = method.lower().replace(" ", "").replace("-", ""), fault_type.lower()
    if mk not in METHODS:
        raise OpError(f"Invalid method: {method}. Options: iec60909, vde, ansi, complete, iec61363, iec61660, "
                      "ieee946, vde_dc")
    if fk not in FAULTS:
        raise OpError(f"Invalid fault_type: {fault_type}. Options: 3ph, 2ph, 1ph, 2phg, 3ph_unbalanced")
    shc = study_command(ctx, "ComShc")
    settings = {"iopt_mde": METHODS[mk], "iopt_shc": FAULTS[fk], "iopt_cur": 0 if current == "max" else 1}
    if rf_ohm is not None:
        settings["Rf"] = rf_ohm
    if xf_ohm is not None:
        settings["Xf"] = xf_ohm
    sel = None
    if isinstance(locations, str) and locations.lower() in ALLBUS:
        settings["iopt_allbus"] = ALLBUS[locations.lower()]
        targets = None
    else:
        targets = resolve_many(ctx, locations)
        if not targets:
            raise OpError("locations does not match any object.")
        if len(targets) == 1:
            settings.update(iopt_allbus=0, shcobj=targets[0])
        else:
            from powerfactory_mcp.ops.results import tmp_folder
            fold = tmp_folder(ctx)
            found = fold.GetContents("pfmcp_shc.SetSelect")
            sel = found[0] if found else fold.CreateObject("SetSelect", "pfmcp_shc")
            sel.Clear()
            sel.AddRef(targets)
            settings.update(iopt_allbus=0, shcobj=sel)
    settings.update(options or {})
    with temp_attrs(ctx, shc, settings):
        ret = shc.Execute()
        if ret != 0:
            raise OpError(f"Short-circuit calculation failed (code {ret}); see errors in messages.")
        rows = _bus_results(ctx, targets, FAULTS[fk])
        contrib = _contributions(ctx, targets) if branch_contributions else None
    cols = ["bus", "grid", "Un_kV", "Ikss_kA", "Skss_MVA", "ip_kA", "Ib_kA", "Ith_kA", "R_ohm", "X_ohm", "X/R"]
    if FAULTS[fk] != "3psc":
        cols += ["IkssA_kA", "IkssB_kA", "IkssC_kA", "3I0_kA"]
    ik = cols.index("Ikss_kA")
    rows.sort(key=lambda r: -(r[ik] or 0))
    out = {
        "method": mk, "fault_type": fk, "current": current, "faulted_buses": len(rows),
        "max": dict(zip(["bus", "Ikss_kA"], [rows[0][0], rows[0][ik]])) if rows else None,
        **table(cols, rows, limit),
    }
    if mk == "ansi" and rows and all(r[ik] is None for r in rows):
        out["note"] = ("With ANSI the IEC variables (Ikss, ip...) are empty; use find_objects with the "
                       "ANSI variables you need or run_python.")
    if contrib:
        out["branch_contributions"] = contrib
    if export:
        p = ctx.out_path(f"short_circuit_{fk}", "csv", export if isinstance(export, str) else None)
        out["file"] = str(write_table(ctx, p, cols, rows))
    return out


def _bus_results(ctx, targets, fault: str) -> list[list]:
    buses = targets if targets else find(ctx, "*.ElmTerm", "calc", include_oos=False)
    rows = []
    for t in buses:
        if t.GetClassName() != "ElmTerm":
            continue
        ikss = get_attr(t, "m:Ikss")
        phases = [get_attr(t, f"m:Ikss:{ph}") for ph in "ABC"]
        if fault != "3psc":
            vals = [p for p in phases if isinstance(p, (int, float))]
            ikss = max(vals) if vals else ikss
        if ikss is None or (isinstance(ikss, float) and ikss == 0 and fault == "3psc"):
            if get_attr(t, "m:Skss") is None:
                continue
        r, x = get_attr(t, "m:R"), get_attr(t, "m:X")
        xr = (x / r) if isinstance(r, (int, float)) and isinstance(x, (int, float)) and r else None
        row = [t.loc_name, grid_of(t), rnd(get_attr(t, "uknom"), 3), rnd(ikss, 4), rnd(get_attr(t, "m:Skss"), 2),
               rnd(get_attr(t, "m:ip"), 4), rnd(get_attr(t, "m:Ib"), 4), rnd(get_attr(t, "m:Ith"), 4),
               rnd(r, 4), rnd(x, 4), rnd(xr, 2)]
        if fault != "3psc":
            row += [rnd(p, 4) for p in phases] + [rnd(get_attr(t, "m:I0x3"), 4)]
        rows.append(row)
    return rows


def _contributions(ctx, targets) -> dict | None:
    """Current contributions of the branches connected to each faulted bus (only with a list of buses)."""
    if not targets:
        return {"note": "Contributions are only calculated with a list of buses in locations."}
    out = {}
    for t in targets[:10]:
        rows = []
        for e in t.GetConnectedElements(0, 0, 0):
            for side in ("bus1", "bus2", "bushv", "busmv", "buslv"):
                if not e.HasAttribute(f"m:Ikss:{side}"):
                    continue
                node_attr = get_attr(e, side)
                if node_attr is None or node_attr.GetParent() is None or node_attr.GetParent() != t:
                    continue
                rows.append([oname(e), side, rnd(get_attr(e, f"m:Ikss:{side}"), 4),
                             rnd(get_attr(e, f"m:Skss:{side}"), 2)])
        out[t.loc_name] = table(["element", "side", "Ikss_kA", "Skss_MVA"], rows)
    return out
