"""Model: find and describe objects, query classes, edit attributes, create and delete elements."""

from __future__ import annotations

import functools
from pathlib import Path

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import (
    attr_info, coerce_value, find, get_attr, grid_of, jsonable, oname, rel_path, resolve_many, resolve_one,
    rnd, table, write_table,
)


# Key attributes per class (inputs) and load flow results if they are valid.
KEY_INPUTS = {
    "ElmTerm": ["uknom", "iUsage", "outserv"],
    "ElmLne": ["typ_id", "dline", "nlnum", "outserv"],
    "ElmTr2": ["typ_id", "ntnum", "nntap", "outserv"],
    "ElmTr3": ["typ_id", "ntnum", "n3tap_h", "outserv"],
    "ElmSym": ["typ_id", "ngnum", "pgini", "qgini", "usetp", "ip_ctrl", "av_mode", "outserv"],
    "ElmGenstat": ["sgn", "ngnum", "pgini", "qgini", "av_mode", "outserv"],
    "ElmPvsys": ["sgn", "ngnum", "pgini", "qgini", "av_mode", "outserv"],
    "ElmLod": ["typ_id", "plini", "qlini", "scale0", "outserv"],
    "ElmXnet": ["bustp", "usetp", "pgini", "snss", "snssmin", "outserv"],
    "ElmShnt": ["shtype", "qcapn", "qrean", "ncapa", "outserv"],
    "ElmCoup": ["on_off", "aUsage", "outserv"],
    "StaSwitch": ["on_off", "aUsage"],
    "ElmAsm": ["typ_id", "pgini", "outserv"],
    "ElmRelay": ["typ_id", "outserv"],
}
KEY_RESULTS = {
    "ElmTerm": ["m:u", "m:Ul", "m:phiu"],
    "ElmLne": ["c:loading", "m:P:bus1", "m:Q:bus1", "m:I:bus1", "c:Ploss"],
    "ElmTr2": ["c:loading", "m:P:bushv", "m:Q:bushv", "m:I:bushv", "c:Ploss"],
    "ElmTr3": ["c:loading", "m:P:bushv", "m:Q:bushv", "m:I:bushv", "c:Ploss"],
    "ElmSym": ["m:P:bus1", "m:Q:bus1", "m:u1:bus1"],
    "ElmGenstat": ["m:P:bus1", "m:Q:bus1"],
    "ElmPvsys": ["m:P:bus1", "m:Q:bus1"],
    "ElmLod": ["m:P:bus1", "m:Q:bus1"],
    "ElmXnet": ["m:P:bus1", "m:Q:bus1"],
    "ElmShnt": ["m:Q:bus1"],
    "ElmAsm": ["m:P:bus1", "m:Q:bus1"],
}
BUS_ATTRS = {
    "ElmTr2": ["bushv", "buslv"],
    "ElmTr3": ["bushv", "busmv", "buslv"],
    "ElmTr4": ["bushv", "busmv", "buslv", "buslv2"],
}


def _bus_attrs(o) -> list[str]:
    cls = o.GetClassName()
    if cls in BUS_ATTRS:
        return BUS_ATTRS[cls]
    names = [n for n in ("bus1", "bus2", "bus3") if o.HasAttribute(n)]
    return names


def _results_valid(ctx) -> bool:
    try:
        return bool(ctx.app.IsLdfValid())
    except Exception:  # noqa: BLE001
        return False


@op()
def find_objects(ctx, pattern: str = "*.ElmTerm", attributes: list[str] | None = None, scope: str = "calc",
                 include_out_of_service: bool = True, name_contains: str | None = None, limit: int = 200,
                 sort_by: str | None = None, descending: bool = True, export: str | None = None) -> dict:
    objs = find(ctx, pattern, scope, include_out_of_service)
    if name_contains:
        objs = [o for o in objs if name_contains.lower() in o.loc_name.lower()]
    classes = sorted({o.GetClassName() for o in objs})
    if attributes is None:
        attributes = []
        if len(classes) == 1:
            attributes = list(KEY_INPUTS.get(classes[0], ["outserv"]))
            if _results_valid(ctx):
                attributes += KEY_RESULTS.get(classes[0], [])
    cols = ["name", "class", "grid"] + attributes
    rows = []
    for o in objs:
        row = [o.loc_name, o.GetClassName(), grid_of(o)]
        for a in attributes:
            row.append(rnd(jsonable(get_attr(o, a), ctx), 5))
        rows.append(row)
    if sort_by:
        if sort_by not in cols:
            raise OpError(f"sort_by must be one of the columns: {cols}")
        i = cols.index(sort_by)
        rows.sort(key=lambda r: (r[i] is None, r[i] if not isinstance(r[i], str) else 0), reverse=descending)
        if descending:  # None last
            rows.sort(key=lambda r: r[i] is None)
    out = {"pattern": pattern, "scope": scope, "classes": classes, **table(cols, rows, limit)}
    if export:
        p = write_table(ctx, ctx.out_path("objects", "csv", export if export is not True else None), cols, rows)
        out["file"] = str(p)
    return out


@op()
def describe_object(ctx, object: str, attributes: list[str] | None = None, all_inputs: bool = False) -> dict:
    o = resolve_one(ctx, object)
    cls = o.GetClassName()
    d: dict = {
        "name": o.loc_name, "class": cls, "class_description": _class_desc(ctx, cls),
        "path": rel_path(ctx, o), "grid": grid_of(o),
    }
    try:
        d["out_of_service"] = bool(o.IsOutOfService())
        d["energized"] = bool(o.IsEnergized())
    except Exception:  # noqa: BLE001
        pass
    typ = get_attr(o, "typ_id")
    if typ is not None:
        d["type"] = {"name": oname(typ), "path": rel_path(ctx, typ)}
    # Connections
    if cls == "ElmTerm":
        try:
            d["connected"] = sorted({oname(x) for x in o.GetConnectedElements(0, 0, 0)})[:100]
        except Exception:  # noqa: BLE001
            pass
    else:
        conns = {}
        for i, a in enumerate(_bus_attrs(o)):
            try:
                node = o.GetNode(i)
                conns[a] = oname(node) if node is not None else None
            except Exception:  # noqa: BLE001
                cub = get_attr(o, a)
                conns[a] = oname(cub.GetParent()) if cub is not None else None
        if conns:
            d["terminals"] = conns
    names = list(attributes) if attributes else list(KEY_INPUTS.get(cls, []))
    if not attributes and _results_valid(ctx):
        names += KEY_RESULTS.get(cls, [])
    if all_inputs:
        names = _catalog(ctx).get(cls, [])
    vals = []
    for n in names:
        if not o.HasAttribute(n):
            vals.append({"name": n, "error": "does not exist"})
            continue
        info = attr_info(o, n) if not all_inputs else {"name": n}
        info["value"] = rnd(jsonable(get_attr(o, n), ctx), 6)
        vals.append(info)
    d["attributes"] = vals
    if all_inputs:
        d["note"] = "Values of all input attributes; describe_class gives description and unit."
    return d


def _class_desc(ctx, cls: str) -> str:
    try:
        return ctx.app.GetClassDescription(cls)
    except Exception:  # noqa: BLE001
        return ""


def _catalog(ctx) -> dict[str, list[str]]:
    return _load_catalog(str(Path(ctx.cfg.workspace) / "_ref" / "pf_attributes.tsv"))


@functools.lru_cache(maxsize=2)
def _load_catalog(path: str) -> dict[str, list[str]]:
    cat: dict[str, list[str]] = {}
    if not Path(path).exists():
        return cat
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith(("#", "class\t")):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            cat.setdefault(parts[0], []).append(parts[1])
    return cat


@op()
def describe_class(ctx, class_name: str, contains: str | None = None, limit: int = 120) -> dict:
    """Input attributes of a class (schema catalog) with live description and unit."""
    cat = _catalog(ctx)
    if class_name not in cat:
        close = [c for c in cat if class_name.lower() in c.lower()][:20]
        raise OpError(f"Class '{class_name}' is not in the catalog. Similar: {close}")
    names = cat[class_name]
    rows = []
    for n in names:
        try:
            desc = ctx.app.GetAttributeDescription(class_name, n) or ""
        except Exception:  # noqa: BLE001
            desc = ""
        if contains and contains.lower() not in (n + " " + desc).lower():
            continue
        try:
            unit = ctx.app.GetAttributeUnit(class_name, n) or ""
        except Exception:  # noqa: BLE001
            unit = ""
        rows.append([n, " ".join(desc.split()), unit.strip()])
        if len(rows) >= limit:
            break
    return {"class": class_name, "description": _class_desc(ctx, class_name), "total_attributes": len(names),
            **table(["attribute", "description", "unit"], rows)}


@op()
def set_attributes(ctx, objects, values: dict, dry_run: bool = False, scope: str = "calc") -> dict:
    objs = resolve_many(ctx, objects, scope)
    if not objs:
        raise OpError("No object matches.")
    if not values:
        raise OpError("values is empty.")
    rows = []
    for o in objs:
        for k, v in values.items():
            if not o.HasAttribute(k):
                rows.append([oname(o), k, None, None, f"{o.GetClassName()} has no '{k}'"])
                continue
            old = jsonable(get_attr(o, k), ctx)
            if dry_run:
                rows.append([oname(o), k, old, jsonable(v, ctx), "dry_run"])
                continue
            try:
                o.SetAttribute(k, coerce_value(ctx, o, k, v))
                new = jsonable(get_attr(o, k), ctx)
                rows.append([oname(o), k, old, new, "ok"])
                ctx.log_change({"op": "set", "object": o.GetFullName(), "attr": k, "old": old, "new": new})
            except Exception as e:  # noqa: BLE001
                rows.append([oname(o), k, old, None, f"error: {e}"])
    out = table(["object", "attribute", "old", "new", "status"], rows, 300)
    if not dry_run:
        out["note"] = ("Changes applied (exclusive: they persist; parallel: lost on close). "
                       "To revert, call again with the 'old' values. Change log: " + str(ctx.change_log))
    return out


@op()
def add_element(ctx, class_name: str, name: str, buses: list[str] | None = None, grid: str | None = None,
                type: str | None = None, attributes: dict | None = None, with_switch: bool = True) -> dict:
    """Creates an element in a grid and connects it: creates a cubicle (StaCubic) at each given bus."""
    ctx.project()
    grids = ctx.app.GetProjectFolder("netdat").GetContents("*.ElmNet", 1)
    if grid:
        gl = [g for g in grids if g.loc_name == grid]
        if not gl:
            raise OpError(f"Grid '{grid}' not found. Grids: {[g.loc_name for g in grids]}")
        net = gl[0]
    elif buses:
        first = resolve_one(ctx, buses[0], "ElmTerm")
        net = first.GetParent()
        while net is not None and net.GetClassName() != "ElmNet":
            net = net.GetParent()
        if net is None:
            raise OpError("Could not infer the grid; specify grid.")
    elif len(grids) == 1:
        net = grids[0]
    else:
        raise OpError(f"Specify grid: {[g.loc_name for g in grids]}")
    if net.GetContents(f"{name}.{class_name}"):
        raise OpError(f"{name}.{class_name} already exists in {net.loc_name}.")
    elm = net.CreateObject(class_name, name)
    if elm is None:
        raise OpError(f"PowerFactory could not create an object of class {class_name}.")
    created = [oname(elm)]
    try:
        if buses:
            ports = _bus_attrs(elm)
            if len(buses) > len(ports):
                raise OpError(f"{class_name} has {len(ports)} connections ({ports}); {len(buses)} buses were given.")
            for port, bus in zip(ports, buses):
                term = resolve_one(ctx, bus, "ElmTerm")
                cub = term.CreateObject("StaCubic", f"Cub_{name}_{port}")
                if with_switch:
                    sw = cub.CreateObject("StaSwitch", f"Sw_{name}_{port}")
                    sw.SetAttribute("on_off", 1)
                elm.SetAttribute(port, cub)
                created.append(f"{term.loc_name}\\{cub.loc_name}")
        if type:
            typ = _find_type(ctx, type)
            elm.SetAttribute("typ_id", typ)
        for k, v in (attributes or {}).items():
            elm.SetAttribute(k, coerce_value(ctx, elm, k, v))
    except Exception:
        elm.Delete()
        raise
    ctx.log_change({"op": "add", "object": elm.GetFullName(), "buses": buses, "type": type, "attrs": attributes})
    res = describe_object(ctx, elm.GetFullName())
    res["created"] = created
    res["note"] = ("Created in the data model, not in the single-line diagram: draw it in the GUI if needed. "
                   "delete_objects moves it to the PowerFactory recycle bin.")
    return res


def _find_type(ctx, spec: str):
    try:
        return resolve_one(ctx, spec)
    except OpError:
        pass
    hits = find(ctx, spec, "library")
    if not hits:
        glob = ctx.app.GetGlobalLibrary()
        if glob is not None:
            hits = glob.GetContents(spec if "." in spec else spec + ".Typ*", 1)
    if len(hits) != 1:
        raise OpError(f"Type '{spec}' {'ambiguous' if hits else 'not found'} "
                      f"({[rel_path(ctx, h) for h in hits[:6]]}). Use the full path.")
    return hits[0]


@op()
def delete_objects(ctx, objects, confirm: bool = False, scope: str = "calc") -> dict:
    objs = resolve_many(ctx, objects, scope)
    if not objs:
        raise OpError("No object matches.")
    listing = [rel_path(ctx, o) for o in objs]
    if not confirm:
        return {"would_delete": listing, "count": len(objs),
                "note": "Preview. Repeat with confirm=true to delete (they go to the PowerFactory recycle bin)."}
    done = []
    for o, path in zip(objs, listing):
        full = o.GetFullName()
        ret = o.Delete()
        done.append({"object": path, "ok": ret == 0})
        if ret == 0:
            ctx.log_change({"op": "delete", "object": full})
    return {"deleted": done}
