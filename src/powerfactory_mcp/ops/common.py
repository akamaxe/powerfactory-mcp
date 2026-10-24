"""Worker context and common utilities: names, JSON, object lookup, tables and files."""

from __future__ import annotations

import collections
import contextlib
import csv
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from powerfactory_mcp.ops import OpError

CLASS_SUFFIX = re.compile(r"\.(\*|[A-Z][A-Za-z0-9]*\*?)$")
SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class Ctx:
    """Worker state: app, pf module, configuration and logs."""

    def __init__(self, app, pf, cfg, mode: str):
        self.app = app
        self.pf = pf
        self.cfg = cfg
        self.mode = mode
        self.gui_visible = False
        self.gui_initialized = False  # becomes True on the first Show(): changes the cost of activating projects
        self.last_result = None
        self.ow = app.GetOutputWindow()
        self.history: collections.deque[str] = collections.deque(maxlen=5000)
        self.py_ns: dict | None = None
        self.change_log = Path(cfg.logs_dir) / "changes.jsonl"

    # -------------------------------------------------------- output window
    def begin_op(self) -> None:
        try:
            self.ow.Clear()
        except Exception:  # noqa: BLE001
            pass

    def collect_messages(self, max_each: int = 30) -> dict:
        MT = self.pf.OutputWindow.MessageType
        try:
            lines = [str(x) for x in self.ow.GetContent()]
            errors = [str(x) for x in self.ow.GetContent(MT.Error)]
            warns = [str(x) for x in self.ow.GetContent(MT.Warn)]
        except Exception as e:  # noqa: BLE001
            return {"note": f"could not read the output window: {e}"}
        self.history.extend(lines)
        if not lines:
            return {}
        flagged = set(errors) | set(warns)
        info = [x for x in lines if x not in flagged and x.strip()]
        out: dict[str, Any] = {}
        if errors:
            out["errors"] = _cap(errors, max_each)
        if warns:
            out["warnings"] = _cap(warns, max_each)
        if info:
            out["info"] = _cap(info, 12, tail=True)
        return out

    # ------------------------------------------------------------- context
    def user(self):
        return self.app.GetCurrentUser()

    def project(self, required: bool = True):
        prj = self.app.GetActiveProject()
        if prj is None and required:
            raise OpError("No active project. Use list_projects() and open_project().")
        return prj

    def study_case(self, required: bool = True):
        case = self.app.GetActiveStudyCase()
        if case is None and required:
            raise OpError("No active study case. Use set_active(study_case=...).")
        return case

    def brief(self) -> dict:
        prj = self.app.GetActiveProject()
        case = self.app.GetActiveStudyCase()
        scen = self.app.GetActiveScenario()
        d = {"project": prj.loc_name if prj else None, "study_case": case.loc_name if case else None}
        if scen is not None:
            d["scenario"] = scen.loc_name
        return d

    # --------------------------------------------------------------- files
    def out_path(self, kind: str, ext: str, path: str | None = None) -> Path:
        """Output path: the given one (validated) or results\\<project>\\<date>_<kind>.<ext>."""
        if path:
            p = self.cfg.check_path(path)
        else:
            prj = self.app.GetActiveProject()
            folder = Path(self.cfg.results_dir) / safe_name(prj.loc_name if prj else "no_project")
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            p = folder / f"{stamp}_{kind}.{ext.lstrip('.')}"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def log_change(self, record: dict) -> None:
        record = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **self.brief(), **record}
        self.change_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.change_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cap(items: list[str], n: int, tail: bool = False) -> list[str]:
    if len(items) <= n:
        return items
    kept = items[-n:] if tail else items[:n]
    note = f"... ({len(items) - n} more lines; output_window() shows them)"
    return ([note] + kept) if tail else (kept + [note])


def safe_name(s: str) -> str:
    return SAFE_NAME.sub("_", s).strip() or "unnamed"


# ------------------------------------------------------------------- names
def is_obj(ctx: Ctx, x) -> bool:
    return isinstance(x, ctx.pf.DataObject)


def oname(o) -> str:
    """'Name.Class' (short form used in tables and to look the object up again)."""
    return f"{o.loc_name}.{o.GetClassName()}"


def opath(o) -> str:
    return o.GetFullName()


def rel_path(ctx: Ctx, o) -> str:
    """Path relative to the active project (shorter than the full one)."""
    full = o.GetFullName()
    prj = ctx.app.GetActiveProject()
    if prj is not None:
        base = prj.GetFullName()
        if full.startswith(base):
            return full[len(base):].lstrip("\\") or prj.loc_name
    return full


def jsonable(x: Any, ctx: Ctx | None = None, depth: int = 0) -> Any:
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if isinstance(x, float):
        return None if (math.isnan(x) or math.isinf(x)) else x
    if ctx is not None and is_obj(ctx, x):
        return oname(x)
    if hasattr(x, "loc_name") and hasattr(x, "GetClassName"):
        return oname(x)
    if depth > 6:
        return str(x)
    if isinstance(x, dict):
        return {str(k): jsonable(v, ctx, depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [jsonable(v, ctx, depth + 1) for v in x]
    if hasattr(x, "item") and callable(x.item):  # numpy scalar
        try:
            return jsonable(x.item(), ctx, depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(x, "tolist"):
        return jsonable(x.tolist(), ctx, depth + 1)
    if hasattr(x, "name") and hasattr(x, "value"):  # Enum
        return x.name
    return str(x)


def rnd(v: Any, nd: int = 4) -> Any:
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, nd)
    return v


# ------------------------------------------------------------------ lookup
def with_class(pattern: str) -> str:
    """Adds '.*' if the pattern has no class ('Bus 1' -> 'Bus 1.*')."""
    return pattern if CLASS_SUFFIX.search(pattern) else pattern + ".*"


def find(ctx: Ctx, pattern: str, scope: str = "calc", include_oos: bool = True) -> list:
    """Objects matching `pattern` (wildcards * ?; several separated by commas).

    scope: calc (relevant to the calculation in the active case) | project | study_case | library |
           user (all of the user's projects) | in:<object> (inside an object/folder).
    A full path ('\\\\user.IntUser\\\\...') is looked up directly.
    """
    pattern = pattern.strip()
    if pattern.startswith("\\"):
        o = ctx.user().SearchObject(pattern)
        return [o] if o is not None else []
    if "\\" in pattern and "*" not in pattern and "?" not in pattern:
        # Path relative to the active project (the form returned by the ambiguity messages and rel_path).
        prj = ctx.project()
        o = ctx.user().SearchObject(prj.GetFullName() + "\\" + pattern.lstrip("\\"))
        return [o] if o is not None else []
    pats = [with_class(p.strip()) for p in pattern.split(",") if p.strip()]
    if scope == "calc":
        ctx.study_case()
        return list(ctx.app.GetCalcRelevantObjects(",".join(pats), int(include_oos)))
    if scope == "project":
        root = ctx.project()
    elif scope == "study_case":
        root = ctx.study_case()
    elif scope == "library":
        root = ctx.app.GetProjectFolder("equip")
        if root is None:
            raise OpError("The active project has no equipment library.")
    elif scope == "user":
        root = ctx.user()
    elif scope.startswith("in:"):
        root = resolve_one(ctx, scope[3:])
    else:
        raise OpError(f"Invalid scope: {scope!r} (calc | project | study_case | library | user | in:<object>)")
    out = []
    for p in pats:
        out.extend(root.GetContents(p, 1))
    if not include_oos:
        out = [o for o in out if not _oos(o)]
    return out


def _oos(o) -> bool:
    try:
        return bool(o.IsOutOfService())
    except Exception:  # noqa: BLE001
        return False


def resolve_one(ctx: Ctx, spec: Any, cls: str | None = None):
    """A single object from 'Name', 'Name.Class', a full path or a DataObject."""
    if spec is None or spec == "":
        raise OpError("No object given.")
    if is_obj(ctx, spec):
        return spec
    spec = str(spec).strip()
    if cls and not CLASS_SUFFIX.search(spec) and not spec.startswith("\\"):
        spec = f"{spec}.{cls}"
    if "\\" in spec:
        hits = find(ctx, spec)
    else:
        hits = []
        if ctx.app.GetActiveStudyCase() is not None:
            hits = find(ctx, spec, "calc")
        if not hits and ctx.app.GetActiveProject() is not None:
            hits = find(ctx, spec, "project")
    hits = _dedupe(hits)
    if not hits:
        raise OpError(f"'{spec}' not found. Try find_objects() with wildcards or a full path.")
    if len(hits) > 1:
        exact = [h for h in hits if h.loc_name == spec.split(".")[0]] or hits
        if len(exact) == 1:
            return exact[0]
        # 'T1' matches both the transformer and its type: prefer the network element.
        net = [h for h in exact if h.GetClassName().startswith(("Elm", "Sta", "Rel"))]
        if len(net) == 1:
            return net[0]
        sample = "; ".join(rel_path(ctx, h) for h in hits[:8])
        raise OpError(f"'{spec}' is ambiguous ({len(hits)} matches): {sample}. "
                      "Give the class ('Name.ElmLne') or one of these paths verbatim.")
    return hits[0]


def resolve_many(ctx: Ctx, specs: Any, scope: str = "calc") -> list:
    """List of objects: accepts a list of names/paths or a wildcard pattern."""
    if specs is None:
        return []
    if isinstance(specs, str):
        specs = [specs]
    out = []
    for s in specs:
        s = str(s)
        if any(ch in s for ch in "*?,") and "\\" not in s:
            out.extend(find(ctx, s, scope))
        else:
            out.append(resolve_one(ctx, s))
    return _dedupe(out)


def _dedupe(objs: Iterable) -> list:
    seen, out = set(), []
    for o in objs:
        if o is None:
            continue
        key = o.GetFullName()
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


# -------------------------------------------------------------- attributes
def get_attr(o, name: str, default: Any = None) -> Any:
    try:
        return o.GetAttribute(name)
    except Exception:  # noqa: BLE001 - missing attribute or no results
        return default


def attr_info(o, name: str) -> dict:
    d: dict[str, Any] = {"name": name}
    for key, fn in (("description", o.GetAttributeDescription), ("unit", o.GetAttributeUnit)):
        try:
            d[key] = fn(name)
        except Exception:  # noqa: BLE001
            pass
    try:
        d["type"] = o.GetAttributeType(name).name
    except Exception:  # noqa: BLE001
        pass
    return d


def coerce_value(ctx: Ctx, o, name: str, value: Any) -> Any:
    """Converts object names to DataObject for OBJECT-type attributes."""
    try:
        at = o.GetAttributeType(name)
    except Exception:  # noqa: BLE001
        return value
    T = ctx.pf.DataObject.AttributeType
    if at == T.OBJECT and isinstance(value, str):
        return None if value == "" else resolve_one(ctx, value)
    if at == T.OBJECT_VEC and isinstance(value, list):
        return [resolve_one(ctx, v) if isinstance(v, str) else v for v in value]
    if at in (T.INTEGER, T.INTEGER64) and isinstance(value, bool):
        return int(value)
    return value


@contextlib.contextmanager
def temp_attrs(ctx: Ctx, obj, values: dict | None):
    """Applies attributes to a command for the duration of the block and restores the originals on exit.

    Restoring a command's settings does not invalidate its results (verified in PF 2024).
    """
    old: dict = {}
    try:
        for k, v in (values or {}).items():
            if not obj.HasAttribute(k):
                raise OpError(f"{obj.GetClassName()} has no attribute '{k}'. "
                              f"See describe_class('{obj.GetClassName()}').")
            old[k] = obj.GetAttribute(k)
            obj.SetAttribute(k, coerce_value(ctx, obj, k, v))
        yield obj
    finally:
        # Reverse order: undoes dependencies between attributes (e.g. start/end of a period).
        for k, v in reversed(list(old.items())):
            try:
                obj.SetAttribute(k, v)
            except Exception:  # noqa: BLE001
                pass


def study_command(ctx: Ctx, cls: str):
    cmd = ctx.app.GetFromStudyCase(cls)
    if cmd is None:
        raise OpError(f"Could not get {cls} from the active study case.")
    return cmd


def grid_of(o) -> str | None:
    p = o.GetParent()
    for _ in range(6):
        if p is None:
            return None
        if p.GetClassName() == "ElmNet":
            return p.loc_name
        p = p.GetParent()
    return None


# ------------------------------------------------------------------ tables
def table(columns: list[str], rows: list[list], limit: int | None = None) -> dict:
    """Compact table for the reply: {'columns', 'rows', 'total'}."""
    total = len(rows)
    if limit is not None and total > limit:
        rows = rows[:limit]
    t = {"columns": columns, "rows": rows, "total": total}
    if len(rows) < total:
        t["truncated"] = True
    return t


def write_csv(path: Path, columns: list[str], rows: Iterable[Iterable]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return path


def write_table(ctx: Ctx, path: Path, columns: list[str], rows: list[list]) -> Path:
    """CSV or XLSX depending on the extension."""
    if path.suffix.lower() == ".xlsx":
        import pandas as pd
        pd.DataFrame(rows, columns=columns).to_excel(path, index=False)
        return path
    return write_csv(path, columns, rows)
