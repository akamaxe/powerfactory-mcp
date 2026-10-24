"""Reading results (ElmRes): columns, statistics, CSV/XLSX export and PNG plots."""

from __future__ import annotations

import fnmatch
import math

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import oname, rel_path, rnd, table, write_table

META_PREFIX = "b:"


def find_result(ctx, spec: str | None):
    """ElmRes by name (in the active case), full path, or None = the last one used by the tools."""
    if spec in (None, "", "last"):
        last = getattr(ctx, "last_result", None)
        if last is not None and not last.IsDeleted():
            return last
        if spec == "last":
            raise OpError("There is no recent result yet; give the ElmRes name.")
        spec = None
    case = ctx.study_case()
    if spec and spec.startswith("\\"):
        o = ctx.user().SearchObject(spec)
        if o is None:
            raise OpError(f"{spec} does not exist")
        return o
    allres = case.GetContents("*.ElmRes", 1)
    if spec is None:
        raise OpError(f"Specify result. ElmRes in the case: {[r.loc_name for r in allres]}")
    key = spec.removesuffix(".ElmRes")
    hits = [r for r in allres if r.loc_name == key] or [r for r in allres if key.lower() in r.loc_name.lower()]
    if len(hits) != 1:
        raise OpError(f"ElmRes '{spec}' {'ambiguous' if hits else 'not found'}. In the case: "
                      f"{[r.loc_name for r in allres]}")
    return hits[0]


def load_columns(ctx, res) -> list[dict]:
    res.Load()
    cols = []
    for c in range(res.GetNumberOfColumns()):
        o = res.GetObject(c)
        var = res.GetVariable(c)
        cols.append({
            "index": c,
            "object": o.loc_name if o is not None else None,
            "class": o.GetClassName() if o is not None else None,
            "variable": var,
            "unit": res.GetUnit(c),
            "description": res.GetDescription(c, 1),
            "is_meta": (o is None or o.GetClassName() == "ElmRes") and var.startswith(META_PREFIX),
        })
    return cols


def column_values(ctx, res, index: int | None) -> list | None:
    """Whole column via IntVec (≈300 times faster than GetValue row by row). index -1 = time."""
    if index is None:
        return None
    vec = _tmp_vec(ctx)
    ret = res.GetColumnValues(vec, index)
    if ret != 0:
        return None
    return [None if (isinstance(v, float) and (math.isnan(v))) else v for v in vec.GetAttribute("V")]


def tmp_folder(ctx):
    """'_mcp_tmp' folder at the user's root (outside the projects) for reusable auxiliary objects."""
    user = ctx.user()
    found = user.GetContents("_mcp_tmp.IntFolder")
    return found[0] if found else user.CreateObject("IntFolder", "_mcp_tmp")


def _tmp_vec(ctx):
    vec = getattr(ctx, "_tmp_vec", None)
    if vec is None or vec.IsDeleted():
        holder = tmp_folder(ctx)
        found = holder.GetContents("pfmcp_vec.IntVec")
        vec = found[0] if found else holder.CreateObject("IntVec", "pfmcp_vec")
        ctx._tmp_vec = vec
    return vec


def select_columns(cols: list[dict], variables: list[str] | None) -> list[dict]:
    """Filters 'Object:var', 'Object.Class:var', '*:m:u1', 'G*:s:*'; no filter = all non-meta columns."""
    data = [c for c in cols if not c["is_meta"]]
    if not variables:
        return data
    out = []
    for f in variables:
        if ":" in f:
            obj_pat, var_pat = f.split(":", 1)
        else:
            obj_pat, var_pat = "*", f
        for c in data:
            names = {c["object"] or "", f"{c['object']}.{c['class']}"}
            if any(fnmatch.fnmatchcase(n, obj_pat) for n in names) and fnmatch.fnmatchcase(c["variable"], var_pat):
                if c not in out:
                    out.append(c)
    return out


def label(c: dict) -> str:
    return f"{c['object']}:{c['variable']}" + (f" [{c['unit']}]" if c["unit"] else "")


def stats_for(x: list, y: list) -> dict:
    pts = [(t, v) for t, v in zip(x, y) if v is not None and v < 1e30]
    if not pts:
        return {"min": None, "max": None, "final": None}
    tmin, vmin = min(pts, key=lambda p: p[1])
    tmax, vmax = max(pts, key=lambda p: p[1])
    return {"min": rnd(vmin, 5), "t_min": rnd(tmin, 4), "max": rnd(vmax, 5), "t_max": rnd(tmax, 4),
            "initial": rnd(pts[0][1], 5), "final": rnd(pts[-1][1], 5)}


def plot_columns(ctx, path, x: list, series: list[tuple[dict, list]], title: str, xlabel: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    groups: dict[str, list] = {}
    for c, y in series:
        groups.setdefault(f"{c['variable']} [{c['unit']}]" if c["unit"] else c["variable"], []).append((c, y))
    n = len(groups)
    fig, axes = plt.subplots(n, 1, figsize=(10, max(2.6 * n, 3.2)), sharex=True, squeeze=False)
    for ax, (var, items) in zip(axes[:, 0], groups.items()):
        for c, y in items[:12]:
            yy = [v if v is not None and v < 1e30 else float("nan") for v in y]
            ax.plot(x, yy, lw=1.2, label=c["object"])
        ax.set_ylabel(var)
        ax.grid(True, alpha=0.3)
        if len(items) > 1:
            ax.legend(fontsize=8, ncol=min(4, len(items)), loc="best")
    axes[-1, 0].set_xlabel(xlabel)
    axes[0, 0].set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return str(path)


def read_elmres(ctx, res, variables: list[str] | None = None, export: str | bool | None = None,
                plot: bool = False, max_columns: int = 60, kind: str = "results", title: str | None = None) -> dict:
    cols = load_columns(ctx, res)
    sel = select_columns(cols, variables)
    nrows = res.GetNumberOfRows()
    xname, xunit = res.GetVariable(), res.GetUnit()
    out: dict = {"result": oname(res), "path": rel_path(ctx, res), "rows": nrows, "columns_total": len(cols),
                 "x": f"{xname} [{xunit}]" if xunit else xname}
    if not sel:
        out["available"] = table(["object", "variable", "unit", "description"],
                                 [[c["object"], c["variable"], c["unit"], c["description"]]
                                  for c in cols if not c["is_meta"]], 200)
        out["note"] = "No column matches variables; these are the available ones."
        return out
    x = column_values(ctx, res, -1) or list(range(nrows))
    series = [(c, column_values(ctx, res, c["index"]) or []) for c in sel]
    out["columns_selected"] = len(sel)
    out["stats"] = table(["column", "min", "t_min", "max", "t_max", "initial", "final"],
                         [[label(c)] + list(stats_for(x, y).values()) for c, y in series[:max_columns]])
    if export:
        path = ctx.out_path(kind, "csv", export if isinstance(export, str) else None)
        header = [out["x"]] + [label(c) for c, _ in series]
        rows = [[x[i]] + [y[i] if i < len(y) and y[i] is not None and y[i] < 1e30 else None for _, y in series]
                for i in range(nrows)]
        out["file"] = str(write_table(ctx, path, header, rows))
    if plot:
        png = ctx.out_path(kind, "png", plot if isinstance(plot, str) else None)
        out["plot"] = plot_columns(ctx, png, x, series, title or f"{res.loc_name} ({ctx.brief()['study_case']})",
                                   out["x"])
    res.Release()
    return out


@op()
def read_results(ctx, result: str | None = None, variables: list[str] | None = None,
                 export: str | bool | None = None, plot: bool | str = False, list_only: bool = False) -> dict:
    """Reads an ElmRes from the active case. variables: 'Object:variable' filters with wildcards."""
    res = find_result(ctx, result)
    if list_only:
        cols = load_columns(ctx, res)
        nrows = res.GetNumberOfRows()
        res.Release()
        return {"result": oname(res), "rows": nrows, **table(
            ["object", "class", "variable", "unit", "description"],
            [[c["object"], c["class"], c["variable"], c["unit"], c["description"]] for c in cols if not c["is_meta"]],
            300)}
    ctx.last_result = res
    return read_elmres(ctx, res, variables, export, plot)


@op()
def list_results(ctx) -> dict:
    case = ctx.study_case()
    rows = []
    for r in case.GetContents("*.ElmRes", 1):
        try:
            r.Load()
            rows.append([r.loc_name, r.GetAttribute("calTp"), r.GetNumberOfColumns(), r.GetNumberOfRows()])
            r.Release()
        except Exception:  # noqa: BLE001
            rows.append([r.loc_name, None, None, None])
    return {"study_case": case.loc_name, **table(["result", "calTp", "columns", "rows"], rows)}
