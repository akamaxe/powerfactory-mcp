"""Protection: relay/fuse inventory, tripping times for faults and time-overcurrent curves.

After ComShc with iopt_prot=1, each stage (RelToc, RelIoc, RelDis*...) exposes c:Ttrip (s, per phase;
9999.999 = no trip) and c:Itrip. The device time is the shortest of its active stages.
"""

from __future__ import annotations

from collections import Counter

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import (
    find, get_attr, grid_of, oname, resolve_many, rnd, study_command, table, temp_attrs,
)
from powerfactory_mcp.ops.shortcircuit import FAULTS, METHODS

STAGE_SETTINGS = ("Ipset", "Ipsetr", "Tpset", "Tset", "Tdelay", "pcharac", "idir", "Zm", "phi", "Xmax", "Rmax",
                  "Fset", "Uset", "Usetr", "Kfac")
STAGE_PREFIXES = ("RelToc", "RelIoc", "RelDis", "RelZpol", "RelFrq", "RelUlim", "RelDir", "RelChar", "RelBias",
                  "RelUlimrel", "RelFuse")
NO_TRIP = 9999.0


def _cubicle(dev):
    p = dev.GetParent()
    while p is not None and p.GetClassName() != "StaCubic":
        p = p.GetParent()
    return p


def _location(dev) -> tuple[str | None, str | None]:
    """(terminal, protected element) from the cubicle that contains the device."""
    cub = _cubicle(dev)
    if cub is None:
        return None, None
    term = cub.GetParent()
    elm = get_attr(cub, "obj_id")
    return (term.loc_name if term is not None else None), (oname(elm) if elm is not None else None)


def _top_devices(ctx, pattern: str, include_oos: bool) -> list:
    """Top-level devices (skips relays nested inside another relay)."""
    out = []
    for d in find(ctx, pattern, "calc", include_oos=include_oos):
        parent = d.GetParent()
        if parent is not None and parent.GetClassName() == "ElmRelay":
            continue
        out.append(d)
    return out


def _stage_objs(dev) -> list:
    if dev.GetClassName() == "RelFuse":
        return [dev]
    return [s for s in dev.GetContents("*.Rel*", 1) if s.GetClassName().startswith(STAGE_PREFIXES)]


def _stage_summary(s) -> str:
    parts = []
    for k in STAGE_SETTINGS:
        if s.HasAttribute(k):
            v = get_attr(s, k)
            if v is None or v == "":
                continue
            parts.append(f"{k}={oname(v) if hasattr(v, 'loc_name') else rnd(v, 4)}")
    return f"{s.loc_name}[{s.GetClassName()}] " + " ".join(parts)


@op()
def list_protection(ctx, pattern: str = "*.ElmRelay,*.RelFuse", include_settings: bool = True,
                    limit: int = 60) -> dict:
    devs = _top_devices(ctx, pattern, include_oos=True)
    rows = []
    for d in devs[:limit]:
        term, elm = _location(d)
        typ = get_attr(d, "typ_id")
        row = [d.loc_name, d.GetClassName(), oname(typ) if typ else None, grid_of(d), term, elm,
               not bool(get_attr(d, "outserv", 0))]
        if include_settings:
            stages = [_stage_summary(s) for s in _stage_objs(d)
                      if not get_attr(s, "outserv", 0) and s is not d] if d.GetClassName() != "RelFuse" else []
            if len(stages) > 8:
                stages = stages[:8] + [f"... (+{len(stages) - 8} stages; describe_object for details)"]
            row.append(stages)
        rows.append(row)
    cols = ["device", "class", "type", "grid", "terminal", "protected_element", "in_service"]
    if include_settings:
        cols.append("active_stages")
    in_service = sum(1 for r in rows if r[6])
    return {"devices_total": len(devs), "in_service": in_service,
            "types_top": Counter(r[2] for r in rows).most_common(10), **table(cols, rows, limit)}


def _device_trip(dev) -> tuple[float | None, str | None, float | None]:
    """(t_trip_s, tripping stage, I_trip) of the device after the short circuit."""
    best = (None, None, None)
    for s in _stage_objs(dev):
        if get_attr(s, "outserv", 0):
            continue
        tt = get_attr(s, "c:Ttrip")
        vals = tt if isinstance(tt, list) else [tt]
        times = [v for v in vals if isinstance(v, (int, float)) and 0 <= v < NO_TRIP]
        if not times:
            continue
        t = min(times)
        if best[0] is None or t < best[0]:
            it = get_attr(s, "c:Itrip")
            ival = max(it) if isinstance(it, list) and it else it
            best = (t, s.loc_name, ival)
    return best


@op()
def protection_study(ctx, fault_locations, fault_type: str = "3ph", method: str = "iec60909",
                     devices: str = "*.ElmRelay,*.RelFuse", only_tripping: bool = True,
                     plot_devices: list[str] | None = None) -> dict:
    mk, fk = method.lower().replace(" ", ""), fault_type.lower()
    if mk not in METHODS or fk not in FAULTS:
        raise OpError("Invalid method or fault_type (see run_short_circuit).")
    shc = study_command(ctx, "ComShc")
    buses = resolve_many(ctx, fault_locations)
    if not buses:
        raise OpError("fault_locations does not match any bus.")
    devs = _top_devices(ctx, devices, include_oos=False)
    if not devs:
        raise OpError("No in-service protection devices match 'devices'.")
    results = {}
    for bus in buses[:20]:
        settings = {"iopt_mde": METHODS[mk], "iopt_shc": FAULTS[fk], "iopt_allbus": 0, "shcobj": bus,
                    "iopt_prot": 1, "iopt_cnf": 0}
        with temp_attrs(ctx, shc, settings):
            ret = shc.Execute()
            if ret != 0:
                raise OpError(f"Short circuit at {bus.loc_name} failed (code {ret}).")
            rows = []
            for d in devs:
                t, stage, itrip = _device_trip(d)
                if t is None and only_tripping:
                    continue
                term, elm = _location(d)
                rows.append([d.loc_name, term, elm, stage, rnd(t, 4), rnd(itrip, 4)])
            rows.sort(key=lambda r: (r[4] is None, r[4] if r[4] is not None else 0))
            results[bus.loc_name] = {
                "Ikss_kA": rnd(get_attr(bus, "m:Ikss"), 4),
                "devices": table(["device", "terminal", "protected_element", "stage", "t_trip_s", "I_trip"],
                                 rows, 80),
            }
            if rows and rows[0][4] is not None:
                results[bus.loc_name]["first_to_trip"] = rows[0][0]
                if len(rows) > 1 and rows[1][4] is not None:
                    results[bus.loc_name]["margin_to_next_s"] = rnd(rows[1][4] - rows[0][4], 4)
    out = {"method": mk, "fault_type": fk, "devices_considered": len(devs), "faults": results,
           "note": "t_trip_s = shortest c:Ttrip of each device's active stages (ComShc with protection). "
                   "It is the operating time for the initial fault; successive openings are not simulated "
                   "(for that, use run_dynamic_simulation with the relays modeled)."}
    if plot_devices:
        try:
            marks = {}
            plotted = {d.loc_name: d for d in resolve_many(ctx, plot_devices)}
            for bus, res in results.items():
                for row in res["devices"]["rows"]:
                    dev = plotted.get(row[0])
                    if dev is not None and row[5]:
                        marks[f"fault {bus} ({row[0]})"] = row[5] * _ct_ratio(dev)
            out["tcc_plot"] = tcc_plot(ctx, plot_devices, fault_currents_A=marks)
        except Exception as e:  # noqa: BLE001 - the curve is optional: the times already computed are kept
            out["tcc_plot_error"] = f"{type(e).__name__}: {e}"
    return out


# Standard curves: t = TMS * (k / (M**a - 1) + c), with M = I / Ipset.
CURVES = {
    "iec_standard": (0.14, 0.02, 0.0), "iec_very": (13.5, 1.0, 0.0), "iec_extremely": (80.0, 2.0, 0.0),
    "iec_long": (120.0, 1.0, 0.0),
    "ieee_moderately": (0.0515, 0.02, 0.114), "ieee_very": (19.61, 2.0, 0.491), "ieee_extremely": (28.2, 2.0, 0.1217),
}


def classify_curve(name: str) -> str | None:
    n = (name or "").lower()
    # "definido": curve names in databases created with the Spanish-language PowerFactory library.
    if "definite" in n or "definido" in n or "constant" in n:
        return "definite"
    fam = "ieee" if any(k in n for k in ("ieee", "ansi", "us ")) else "iec" if any(
        k in n for k in ("iec", "bs 142", "class a", "class b", "class c")) else None
    if fam is None:
        return None
    if "extrem" in n or "class c" in n:
        return f"{fam}_extremely"
    if "very" in n or "class b" in n:
        return f"{fam}_very"
    if "long" in n:
        return "iec_long" if fam == "iec" else None
    if "moder" in n:
        return "ieee_moderately"
    if "standard" in n or "normal" in n or "class a" in n or "inverse" in n:
        return "iec_standard" if fam == "iec" else "ieee_moderately"
    return None


def stage_curve(kind: str, ipset: float, tms: float, i_values: list[float]) -> list[float | None]:
    """Stage times for currents in the same units as ipset (None = no pickup)."""
    out = []
    for i in i_values:
        m = i / ipset if ipset else 0
        if m <= 1.0:
            out.append(None)
        elif kind == "definite":
            out.append(tms)
        else:
            k, a, c = CURVES[kind]
            out.append(tms * (k / (m ** a - 1) + c))
    return out


def _ct_ratio(dev) -> float:
    cub = _cubicle(dev)
    for ct in (cub.GetContents("*.StaCt") if cub is not None else []):
        p, s = get_attr(ct, "ptapset"), get_attr(ct, "stapset")
        if p and s:
            return float(p) / float(s)
    p, s = get_attr(dev, "ctprim"), get_attr(dev, "ctsec")
    return float(p) / float(s) if p and s else 1.0


def tcc_plot(ctx, device_specs: list[str], path: str | None = None,
             fault_currents_A: dict | None = None) -> str:
    """Own time-overcurrent curve (matplotlib) with the relays' RelToc/RelIoc stages, in primary A.

    Supports IEC 60255 / IEEE C37.112 curves and definite time. GrpPage.GetOrInsertPlot(…, 10) does not create a
    PltOvercurrent in PF 2024 (it returns PltLinebarplot), which is why the native plot is not used."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    devs = resolve_many(ctx, device_specs)
    if not devs:
        raise OpError("plot_devices does not match any device.")
    fig, ax = plt.subplots(figsize=(9, 7))
    skipped, drawn = [], 0
    for dev in devs:
        ratio = _ct_ratio(dev)
        stages = [s for s in _stage_objs(dev) if s.GetClassName() in ("RelToc", "RelIoc")
                  and not get_attr(s, "outserv", 0)]
        pts_i, pts_t = [], []
        for s in stages:
            ipset = get_attr(s, "Ipset")
            if not ipset:
                continue
            if s.GetClassName() == "RelIoc":
                kind, tms = "definite", get_attr(s, "Tset") or 0.0
            else:
                ch = get_attr(s, "pcharac")
                kind = classify_curve(ch.loc_name if ch is not None else "")
                tms = get_attr(s, "Tpset") or 0.0
                if kind is None:
                    skipped.append(f"{dev.loc_name}/{s.loc_name}: curve '{ch.loc_name if ch else '?'}' not supported")
                    continue
            isec = np.logspace(np.log10(ipset * 1.001), np.log10(ipset * 40), 200)
            ts = stage_curve(kind, ipset, tms, list(isec))
            pts_i.append(isec * ratio)
            pts_t.append(np.array([t if t is not None else np.nan for t in ts]))
        if not pts_i:
            continue
        # Combined relay characteristic: minimum of the stages at each current.
        grid = np.logspace(np.log10(min(a[0] for a in pts_i)), np.log10(max(a[-1] for a in pts_i)), 400)
        comb = np.full_like(grid, np.nan)
        for ii, tt in zip(pts_i, pts_t):
            interp = np.interp(grid, ii, tt, left=np.nan, right=np.nan)
            comb = np.fmin(comb, interp)
        ax.loglog(grid, comb, lw=1.8, label=f"{dev.loc_name} (CT {ratio:g}:1)")
        drawn += 1
    if not drawn:
        plt.close(fig)
        raise OpError("No relay has RelToc/RelIoc stages with supported curves. " + "; ".join(skipped[:5]))
    ax.set_xlabel("Current [primary A]")
    ax.set_ylabel("Time [s]")
    ax.set_title(f"Time-overcurrent coordination – {ctx.brief()['study_case']}")
    ax.grid(True, which="both", alpha=0.3)
    ax.set_ylim(0.01, 1000)
    for name, ia in (fault_currents_A or {}).items():
        ax.axvline(ia, ls="--", lw=1, color="gray")
        ax.text(ia, 600, f" {name}", rotation=90, va="top", fontsize=8)
    ax.legend(fontsize=8)
    fig.tight_layout()
    dst = ctx.out_path("tcc_coordination", "png", path)
    fig.savefig(dst, dpi=110)
    plt.close(fig)
    if skipped:
        ctx.app.PrintWarn("TCC curve: " + "; ".join(skipped[:5]))
    return str(dst)
