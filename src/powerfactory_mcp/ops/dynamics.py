"""RMS/EMT dynamic simulation (ComInc + ComSim) with own events and monitored variables."""

from __future__ import annotations

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import find, get_attr, oname, resolve_many, resolve_one, rnd, study_command, temp_attrs
from powerfactory_mcp.ops.results import column_values, load_columns, read_elmres

SIM_TYPES = {"rms": "rms", "emt": "ins", "ins": "ins"}
NETWORKS = {"balanced": "sym", "sym": "sym", "unbalanced": "rst", "rst": "rst"}
# EvtShc.i_shc (verified: 4 = clear short circuit).
SHC_CODES = {"3ph": 0, "2ph": 1, "1ph": 2, "slg": 2, "2phg": 3, "llg": 3}
EVENTS_FOLDER = "MCP Events"
RESULTS_NAME = "MCP Results"
AUTO_GEN_VARS = ["s:xspeed", "c:firel", "s:P1", "s:Q1"]
AUTO_BUS_VARS = ["m:u1"]


def _event_folder(ctx, case):
    found = case.GetContents(f"{EVENTS_FOLDER}.IntEvt")
    fold = found[0] if found else case.CreateObject("IntEvt", EVENTS_FOLDER)
    for e in fold.GetContents():
        e.Delete()
    return fold


def _make_event(ctx, fold, i: int, spec: dict):
    kind = str(spec.get("type", "")).lower()
    t = float(spec.get("time", 0.0))
    target = resolve_one(ctx, spec["target"]) if spec.get("target") else None
    if kind in ("short_circuit", "fault", "shc"):
        ev = fold.CreateObject("EvtShc", f"{i:02d} fault {target.loc_name}")
        ft = str(spec.get("fault_type", "3ph")).lower()
        if ft not in SHC_CODES:
            raise OpError(f"Invalid event fault_type: {ft} ({', '.join(SHC_CODES)})")
        ev.SetAttribute("i_shc", SHC_CODES[ft])
        ev.SetAttribute("R_f", float(spec.get("rf_ohm", 0.0)))
        ev.SetAttribute("X_f", float(spec.get("xf_ohm", 0.0)))
        if "location_pct" in spec:
            ev.SetAttribute("shcLocation", float(spec["location_pct"]))
    elif kind in ("clear", "clear_fault"):
        ev = fold.CreateObject("EvtShc", f"{i:02d} clear {target.loc_name}")
        ev.SetAttribute("i_shc", 4)
    elif kind in ("switch", "open", "close"):
        ev = fold.CreateObject("EvtSwitch", f"{i:02d} switch {target.loc_name}")
        action = spec.get("action", "close" if kind == "close" else "open")
        ev.SetAttribute("i_switch", 1 if action == "close" else 0)
    elif kind in ("param", "parameter"):
        ev = fold.CreateObject("EvtParam", f"{i:02d} param {target.loc_name}")
        ev.SetAttribute("variable", str(spec["variable"]))
        ev.SetAttribute("value", str(spec["value"]))
    elif kind in ("load", "load_step"):
        ev = fold.CreateObject("EvtLod", f"{i:02d} load {target.loc_name}")
        ev.SetAttribute("dP", float(spec.get("dP_pct", 0.0)))
        ev.SetAttribute("dQ", float(spec.get("dQ_pct", 0.0)))
    elif kind in ("outage", "trip"):
        ev = fold.CreateObject("EvtOutage", f"{i:02d} outage {target.loc_name}")
        if "i_what" in spec:
            ev.SetAttribute("i_what", int(spec["i_what"]))
    elif kind in ("gen", "generator_step"):
        ev = fold.CreateObject("EvtGen", f"{i:02d} gen {target.loc_name}")
        ev.SetAttribute("dP", float(spec.get("dP", 0.0)))
    else:
        raise OpError(f"Unknown event type: {kind!r} "
                      "(short_circuit, clear, switch, param, load, outage, gen)")
    if target is not None:
        ev.SetAttribute("p_target", target)
    ev.SetAttribute("time", t)
    for k, v in (spec.get("attributes") or {}).items():
        ev.SetAttribute(k, v)
    return ev


def _results_object(ctx, case, monitor) -> object:
    found = case.GetContents(f"{RESULTS_NAME}.ElmRes")
    res = found[0] if found else case.CreateObject("ElmRes", RESULTS_NAME)
    for m in res.GetContents("*.IntMon"):
        m.Delete()
    specs: list[tuple[str, list[str]]] = []
    if monitor == "auto":
        gens = find(ctx, "*.ElmSym", "calc", include_oos=False)
        specs += [(g.GetFullName(), AUTO_GEN_VARS) for g in gens]
        buses = [b for b in find(ctx, "*.ElmTerm", "calc", include_oos=False) if (get_attr(b, "iUsage") or 0) == 0]
        specs += [(b.GetFullName(), AUTO_BUS_VARS) for b in buses[:60]]
    elif isinstance(monitor, dict):
        specs += [(k, v if isinstance(v, list) else [v]) for k, v in monitor.items()]
    else:
        raise OpError("monitor must be 'auto', a dict {object: [variables]} or a list of 'Object:variable'.")
    added = 0
    for pattern, variables in specs:
        for o in resolve_many(ctx, pattern):
            for v in variables:
                res.AddVariable(o, v)
                added += 1
    if not added:
        raise OpError("monitor did not add any variable.")
    return res


def _split_monitor_item(item: str) -> tuple[str, str]:
    # 'Bus 5:m:u1' -> ('Bus 5', 'm:u1'); 'G1.ElmSym:s:xspeed' -> ('G1.ElmSym', 's:xspeed')
    parts = item.split(":")
    if len(parts) < 3:
        raise OpError(f"monitor: use 'Object:variable' with the variable prefix, e.g. 'Bus 5:m:u1' ({item!r})")
    return ":".join(parts[:-2]), ":".join(parts[-2:])


@op()
def run_dynamic_simulation(ctx, sim_type: str = "rms", t_end: float = 5.0, step_s: float | None = None,
                           events: list[dict] | None = None, monitor: dict | list | str | None = "auto",
                           network: str = "balanced", t_start: float | None = None,
                           options: dict | None = None, plot: bool = True, export: bool = True,
                           variables_report: list[str] | None = None) -> dict:
    """events = [{'type': 'short_circuit', 'target': 'Bus 7', 'time': 0.1, 'fault_type': '3ph'},
                 {'type': 'clear', 'target': 'Bus 7', 'time': 0.2}, ...]  (None = study case events).
    monitor = 'auto' | {'Object or pattern': ['var', ...]} | ['Obj:var', ...] | None (= study case ElmRes)."""
    st, net = SIM_TYPES.get(sim_type.lower()), NETWORKS.get(network.lower())
    if st is None or net is None:
        raise OpError("sim_type: rms | emt; network: balanced | unbalanced")
    case = ctx.study_case()
    inc, sim = study_command(ctx, "ComInc"), study_command(ctx, "ComSim")
    inc_set: dict = {"iopt_sim": st, "iopt_net": net, **(options or {})}
    if step_s:
        inc_set["dtemt" if st == "ins" else "dtgrd"] = step_s
    if t_start is not None:
        inc_set["tstart"] = t_start
    event_list = []
    if events is not None:
        fold = _event_folder(ctx, case)
        event_list = [oname(_make_event(ctx, fold, i, e)) for i, e in enumerate(events, 1)]
        inc_set["p_event"] = fold
    if monitor:
        if isinstance(monitor, list):
            monitor = _list_to_dict(monitor)
        res = _results_object(ctx, case, monitor)
        inc_set["p_resvar"] = res
    else:
        res = get_attr(inc, "p_resvar")
        if res is None:
            raise OpError("The study case has no ElmRes in ComInc: use monitor='auto' or a dict of variables.")
    with temp_attrs(ctx, inc, inc_set), temp_attrs(ctx, sim, {"tstop": t_end}):
        r1 = inc.Execute()
        if r1 != 0:
            raise OpError(f"Initial conditions calculation failed (ComInc={r1}). Check the errors in messages "
                          "(uninitialized dynamic models, non-converging load flow...).")
        r2 = sim.Execute()
        ctx.last_result = res
        out = {"sim_type": sim_type, "t_end": t_end, "return_codes": {"ComInc": r1, "ComSim": r2},
               "events": event_list or "study case events", "result_file": oname(res)}
        out.update(read_elmres(ctx, res, variables_report, export=export, plot=plot,
                               kind=f"sim_{sim_type}", title=f"{sim_type.upper()} – {case.loc_name}"))
        out["indicators"] = _indicators(ctx, res)
    if r2 != 0:
        out["warning"] = f"ComSim returned {r2}: the simulation ended before t_end (see messages)."
    return out


def _list_to_dict(items: list) -> dict:
    d: dict = {}
    for it in items:
        obj, var = _split_monitor_item(str(it))
        d.setdefault(obj, []).append(var)
    return d


def _indicators(ctx, res) -> dict:
    """Quick stability indicators over the monitored columns."""
    cols = load_columns(ctx, res)
    x = column_values(ctx, res, -1) or []
    ind: dict = {}
    ang = [(c, column_values(ctx, res, c["index"])) for c in cols if c["variable"] == "c:firel"]
    if len(ang) >= 2 and x:
        spread = []
        for i in range(len(x)):
            vals = [v[i] for _, v in ang if v and v[i] is not None and v[i] < 1e30]
            spread.append(max(vals) - min(vals) if len(vals) >= 2 else 0)
        k = max(range(len(spread)), key=lambda i: spread[i])
        ind["max_rotor_angle_spread_deg"] = rnd(spread[k], 2)
        ind["t_max_spread_s"] = rnd(x[k], 3)
        ind["final_rotor_angle_spread_deg"] = rnd(spread[-1], 2)
        ind["angle_stability"] = "probable loss of synchronism" if spread[k] > 180 else "angle stable"
    spd = [(c, column_values(ctx, res, c["index"])) for c in cols if c["variable"] == "s:xspeed"]
    if spd:
        fnom = get_attr(ctx.app.GetProjectFolder("netdat").GetContents("*.ElmNet", 1)[0], "frnom") or 60.0
        mins = [(min(v for v in s if v is not None and v < 1e30), c["object"]) for c, s in spd if s]
        maxs = [(max(v for v in s if v is not None and v < 1e30), c["object"]) for c, s in spd if s]
        lo, hi = min(mins), max(maxs)
        ind["speed_min_pu"] = [rnd(lo[0], 5), lo[1]]
        ind["speed_max_pu"] = [rnd(hi[0], 5), hi[1]]
        ind["freq_nadir_Hz"] = rnd(lo[0] * fnom, 3)
        ind["freq_peak_Hz"] = rnd(hi[0] * fnom, 3)
    volt = [(c, column_values(ctx, res, c["index"])) for c in cols if c["variable"] in ("m:u1", "m:u")]
    if volt:
        mins = [(min(v for v in s if v is not None and v < 1e30), c["object"]) for c, s in volt if s]
        finals = [(next((v for v in reversed(s) if v is not None and v < 1e30), None), c["object"]) for c, s in volt if s]
        ind["voltage_min_pu"] = [rnd(min(mins)[0], 4), min(mins)[1]]
        low_final = [f for f in finals if f[0] is not None and f[0] < 0.9]
        ind["buses_below_0.9pu_at_end"] = [f[1] for f in low_final][:20]
    res.Release()
    return ind
