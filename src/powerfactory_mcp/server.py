"""MCP server 'powerfactory': tools to analyze, edit and simulate in DIgSILENT PowerFactory 2024.

Each tool delegates to the worker process (client.WorkerClient), which hosts the PowerFactory engine.
"""

from __future__ import annotations

import atexit
import functools
import json
import sys
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from powerfactory_mcp import __version__
from powerfactory_mcp.client import Busy, WorkerClient, WorkerError
from powerfactory_mcp.config import PathNotAllowed, load_config

INSTRUCTIONS = """\
DIgSILENT PowerFactory 2024 through its Python API. The engine runs in a worker process of the server.

MODES: 'exclusive' (default) works on the real local database, so changes persist and the user's GUI must be
closed. 'parallel' works on an in-memory copy (/inMemory) that coexists with the open GUI; changes are lost on
disconnect unless exported with export_project. connect(mode=...) switches mode.
The engine starts on the first call (about 20 s in exclusive mode, about 45 s in parallel mode) and is released
after a period of inactivity.

WORKFLOW: list_projects → open_project(project, study_case) → project_overview → studies (run_load_flow,
run_short_circuit, run_contingency_analysis, run_dynamic_simulation, protection, run_modal_analysis...) →
read_results / export_diagram to review results and diagrams (PNGs can be read with Read).
To edit the model: find_objects / describe_object → set_attributes (change log with previous values),
add_element, delete_objects. Before large changes in exclusive mode, use export_project (.pfd backup).

NAMES: objects are referenced as 'Name', 'Name.Class' ('Line 4-5.ElmLne') or by their full path.
Patterns accept wildcards and commas ('*.ElmLne,*.ElmTr2'). Attributes use PowerFactory names:
input 'plini'; result 'm:u', 'c:loading', 'm:P:bus1'; simulation 's:xspeed'.
describe_class(class) lists the input attributes with description and unit. The full catalog is in
<REF>\\pf_attributes.tsv and the API reference in <REF>\\PythonReference_en.txt (plain text, searchable with Grep).

Typed studies restore the command settings (ComLdf, ComShc...) when they finish. For everything
else there is run_python: code with app, pf and helpers in the namespace; assign `result` to return data.
Every response includes the new messages from the PowerFactory output window (errors and warnings first).
Long studies return a job_id: poll it with jobs(action='wait', job_id=...).
"""

_cfg = load_config()
mcp = MCPServer(name="powerfactory", version=__version__,
                instructions=INSTRUCTIONS.replace("<REF>", str(_cfg.workspace / "_ref")))
_client = WorkerClient(_cfg)
atexit.register(_client.shutdown)

MAX_CHARS = 60000
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False)
EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _format(reply: dict) -> str:
    if "job" in reply:
        return _dump({"job": reply["job"], "note": reply.get("note")})
    if not reply.get("ok"):
        msg = reply.get("error", "unknown error")
        extra = {k: reply[k] for k in ("messages", "context") if reply.get(k)}
        if reply.get("traceback"):
            extra["traceback"] = reply["traceback"][-1500:]
        raise ToolError(msg + ("\n" + _dump(extra) if extra else ""))
    out = {"result": reply.get("result")}
    if reply.get("messages"):
        out["pf_messages"] = reply["messages"]
    if reply.get("context"):
        out["context"] = reply["context"]
    out["elapsed_s"] = reply.get("elapsed_s")
    text = _dump(out)
    if len(text) > MAX_CHARS:  # Claude Code's MCP output limit is around 25,000 tokens
        return _dump({"truncated": True, "chars": len(text),
                      "note": "Response too large: lower 'limit', filter, or use export=true to get the file.",
                      "preview": text[:MAX_CHARS]})
    return text


def _call(op: str, args: dict, *, timeout: float | None = None, long: bool = False) -> str:
    try:
        return _format(_client.call(op, {k: v for k, v in args.items() if v is not None},
                                    timeout=timeout, long=long))
    except (WorkerError, Busy, PathNotAllowed) as e:
        raise ToolError(str(e)) from None


def tool(annotations: ToolAnnotations = RO):
    """Registers the tool and turns any error into a ToolError with the real message."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"{type(e).__name__}: {e}") from None
        return mcp.tool(annotations=annotations)(wrapper)
    return deco


# ==================================================================== session
@tool()
def status() -> str:
    """Server and PowerFactory status: connected or not, mode, license, active project and study case,
    valid results, user's GUI open, running job. Does not start the engine if it is stopped."""
    state = _client.state()
    if state["connected"] and not state["running_job"]:
        try:
            state["powerfactory"] = _client.call("status", {}, autostart=False).get("result")
        except (WorkerError, Busy) as e:
            state["powerfactory_error"] = str(e)
    return _dump(state)


@tool(EDIT)
def connect(mode: Annotated[Literal["exclusive", "parallel"] | None,
                            Field(description="exclusive = real DB (GUI closed); parallel = in-memory copy")] = None,
            show_gui: Annotated[bool, Field(description="show the engine window (read-only)")] = False) -> str:
    """Starts PowerFactory or switches mode (restarts the engine if the mode differs)."""
    try:
        info = _client.start(mode, show_gui)
    except (WorkerError, Busy) as e:
        raise ToolError(str(e)) from None
    return _dump({"connected": True, "mode": _client.mode, "info": info})


@tool(EDIT)
def disconnect() -> str:
    """Shuts down the PowerFactory engine and releases the local database (so the user can open their GUI)."""
    try:
        _client.stop("disconnect requested")
    except Busy as e:
        raise ToolError(str(e)) from None
    return _dump({"connected": False})


@tool(EDIT)
def show_gui(visible: bool = True) -> str:
    """Shows or hides the engine window ([NON-INTERACTIVE MODE]: useful for watching what Claude does)."""
    return _call("show_gui", {"visible": visible})


@tool()
def output_window(filter: Annotated[Literal["all", "errors", "warnings"], Field()] = "all",
                  last_n: int = 100, contains: str | None = None) -> str:
    """History of the PowerFactory output window for this session (errors, warnings, info)."""
    return _call("output_window", {"filter": filter, "last_n": last_n, "contains": contains}, timeout=60)


@tool()
def jobs(action: Annotated[Literal["list", "status", "wait", "cancel"], Field()] = "status",
         job_id: str | None = None,
         wait_s: Annotated[float, Field(description="maximum wait for action='wait'", le=3000)] = 120) -> str:
    """Tracks long studies that returned a job_id. cancel kills the engine (the next call restarts it)."""
    try:
        if action == "list":
            return _dump(_client.list_jobs())
        if action == "cancel":
            return _dump(_client.cancel_job(job_id))
        out = _client.wait_job(job_id, wait_s if action == "wait" else 0)
    except WorkerError as e:
        raise ToolError(str(e)) from None
    reply = out.pop("reply", None)
    if reply is not None:
        out["outcome"] = json.loads(_format(reply)) if reply.get("ok") else {"error": reply.get("error"),
                                                                           "messages": reply.get("messages")}
    return _dump(out)


# ==================================================================== project
@tool()
def list_projects(contains: str | None = None) -> str:
    """Projects of the PowerFactory user (relative paths, active project marked)."""
    return _call("list_projects", {"contains": contains})


@tool(EDIT)
def open_project(project: Annotated[str, Field(description="name, relative path from list_projects, or full path")],
                 study_case: str | None = None) -> str:
    """Activates a project (and optionally a study case) and returns its overview."""
    return _call("open_project", {"project": project, "study_case": study_case})


@tool()
def project_overview() -> str:
    """Overview of the active project: study cases, scenarios, variations, grids, diagrams, element counts,
    voltage levels, total generation and load, valid results."""
    return _call("project_overview", {})


@tool(EDIT)
def set_active(study_case: str | None = None, scenario: str | None = None,
               variations: Annotated[list[str] | None, Field(description="variations to leave active")] = None,
               deactivate_scenario: bool = False) -> str:
    """Activates a study case, operation scenario or variations of the active project."""
    return _call("set_active", {"study_case": study_case, "scenario": scenario, "variations": variations,
                                "deactivate_scenario": deactivate_scenario or None})


@tool(EDIT)
def import_project(path: Annotated[str, Field(description=".pfd or .dz file")],
                   target_folder: Annotated[str | None, Field(description="folder under the user")] = None,
                   activate: bool = False) -> str:
    """Imports a .pfd/.dz project into the database (in parallel mode the import is temporary)."""
    return _call("import_project", {"path": path, "target_folder": target_folder, "activate": activate},
                 timeout=1800)


@tool(EDIT)
def export_project(project: str | None = None,
                   path: Annotated[str | None, Field(description="target .pfd; defaults to backups\\<project>_<date>.pfd")] = None) -> str:
    """Exports a project to .pfd (backup). Without arguments, exports the active project to the backups folder."""
    return _call("export_project", {"project": project, "path": path}, timeout=1800)


# ====================================================================== model
@tool()
def find_objects(pattern: Annotated[str, Field(description="'*.ElmTerm', 'Bus*', '*.ElmLne,*.ElmTr2' or path")] = "*.ElmTerm",
                 attributes: Annotated[list[str] | None, Field(description="attributes to read; default: the class's key ones (+ results if the LF is valid)")] = None,
                 scope: Annotated[str, Field(description="calc | project | study_case | library | user | in:<object>")] = "calc",
                 include_out_of_service: bool = True, name_contains: str | None = None,
                 limit: int = 200, sort_by: str | None = None, descending: bool = True,
                 export: Annotated[str | None, Field(description=".csv/.xlsx path to export the full table")] = None) -> str:
    """Finds objects and returns a table with their attributes (inputs or results)."""
    return _call("find_objects", {"pattern": pattern, "attributes": attributes, "scope": scope,
                                  "include_out_of_service": include_out_of_service, "name_contains": name_contains,
                                  "limit": limit, "sort_by": sort_by, "descending": descending, "export": export})


@tool()
def describe_object(object: str, attributes: list[str] | None = None,
                    all_inputs: Annotated[bool, Field(description="values of ALL input attributes")] = False) -> str:
    """Object details: class, path, grid, type, connections (connected buses or elements) and attributes
    with description, unit and value."""
    return _call("describe_object", {"object": object, "attributes": attributes, "all_inputs": all_inputs})


@tool()
def describe_class(class_name: Annotated[str, Field(description="e.g. ComLdf, ElmSym, TypLne, EvtShc")],
                   contains: Annotated[str | None, Field(description="filter by name or description")] = None,
                   limit: int = 120) -> str:
    """Input attributes of a class with description and unit (to know what to set in options/settings)."""
    return _call("describe_class", {"class_name": class_name, "contains": contains, "limit": limit}, timeout=120)


@tool(EDIT)
def set_attributes(objects: Annotated[list[str] | str, Field(description="names, paths or a pattern")],
                   values: Annotated[dict[str, Any], Field(description="{attribute: value}; for references, the object name")],
                   dry_run: bool = False, scope: str = "calc") -> str:
    """Modifies attributes of one or more objects. Returns the old and new values and records them in the
    change log. In exclusive mode the change persists."""
    return _call("set_attributes", {"objects": objects, "values": values, "dry_run": dry_run, "scope": scope})


@tool(EDIT)
def add_element(class_name: Annotated[str, Field(description="ElmLod, ElmLne, ElmTr2, ElmSym, ElmGenstat, ElmShnt, ElmTerm...")],
                name: str,
                buses: Annotated[list[str] | None, Field(description="buses in port order (bus1, bus2 / bushv, buslv)")] = None,
                grid: str | None = None,
                type: Annotated[str | None, Field(description="type (TypLne, TypTr2...) by name or path")] = None,
                attributes: dict[str, Any] | None = None, with_switch: bool = True) -> str:
    """Creates an element and connects it to its buses (cubicle + switch). Does not draw it in the single-line
    diagram."""
    return _call("add_element", {"class_name": class_name, "name": name, "buses": buses, "grid": grid,
                                 "type": type, "attributes": attributes, "with_switch": with_switch})


@tool(DESTRUCTIVE)
def delete_objects(objects: list[str] | str, confirm: bool = False, scope: str = "calc") -> str:
    """Deletes objects (to the PowerFactory recycle bin). Without confirm=true it only shows what it would delete."""
    return _call("delete_objects", {"objects": objects, "confirm": confirm, "scope": scope})


# ==================================================================== studies
@tool(EDIT)
def run_load_flow(method: Annotated[Literal["ac", "unbalanced", "dc"], Field()] = "ac",
                  options: Annotated[dict[str, Any] | None, Field(description="ComLdf attributes, e.g. {'iopt_at':1,'iopt_lim':1,'iopt_lev':1}")] = None,
                  v_min: float = 0.95, v_max: float = 1.05, loading_max: float = 100.0,
                  report: Annotated[Literal["summary", "violations", "full"], Field()] = "violations",
                  limit: int = 200,
                  export: Annotated[str | bool | None, Field(description="true or a .xlsx/.csv path")] = None) -> str:
    """Load flow (ComLdf): convergence, balance (generation, load, losses), voltage violations and
    overloads, most loaded branches, and full tables with report='full'. Gives hints if it does not converge."""
    return _call("run_load_flow", {"method": method, "options": options, "v_min": v_min, "v_max": v_max,
                                   "loading_max": loading_max, "report": report, "limit": limit, "export": export})


@tool(EDIT)
def run_contingency_analysis(elements: Annotated[list[str] | str, Field(description="pattern or element list to outage (N-1)")] = "*.ElmLne,*.ElmTr2,*.ElmTr3",
                             method: Annotated[Literal["ac", "dc"], Field()] = "ac",
                             v_min: float = 0.95, v_max: float = 1.05, loading_max: float = 100.0,
                             top_n: int = 20, export: str | bool | None = None) -> str:
    """N-1 analysis (own ComSimoutage 'MCP N-1'): post-contingency overloads and voltages,
    the worst case per contingency and the non-converged cases."""
    return _call("run_contingency_analysis", {"elements": elements, "method": method, "v_min": v_min,
                                              "v_max": v_max, "loading_max": loading_max, "top_n": top_n,
                                              "export": export}, long=True)


@tool(EDIT)
def run_short_circuit(method: Annotated[str, Field(description="iec60909 | vde | ansi | complete | iec61363 | iec61660 | ieee946")] = "iec60909",
                      fault_type: Annotated[str, Field(description="3ph | 2ph | 1ph | 2phg | 3ph_unbalanced")] = "3ph",
                      locations: Annotated[list[str] | str, Field(description="'busbars', 'all_nodes' or list of buses")] = "busbars",
                      current: Annotated[Literal["max", "min"], Field()] = "max",
                      rf_ohm: float | None = None, xf_ohm: float | None = None,
                      options: Annotated[dict[str, Any] | None, Field(description="other ComShc attributes")] = None,
                      branch_contributions: bool = False, limit: int = 300,
                      export: str | bool | None = None) -> str:
    """Short circuit (ComShc): Ikss, Skss, ip, Ib, Ith, R, X and X/R per bus (phase currents and 3I0 for
    unbalanced faults). With a list of buses, optionally the contribution of each branch."""
    return _call("run_short_circuit", {"method": method, "fault_type": fault_type, "locations": locations,
                                       "current": current, "rf_ohm": rf_ohm, "xf_ohm": xf_ohm, "options": options,
                                       "branch_contributions": branch_contributions, "limit": limit,
                                       "export": export})


@tool(EDIT)
def run_dynamic_simulation(
        sim_type: Annotated[Literal["rms", "emt"], Field()] = "rms",
        t_end: Annotated[float, Field(description="end time [s]")] = 5.0,
        step_s: Annotated[float | None, Field(description="integration step [s]")] = None,
        events: Annotated[list[dict[str, Any]] | None, Field(description=(
            "None = the study case events. If given, they temporarily replace the case events. Types: "
            "{'type':'short_circuit','target':'Bus 7','time':0.1,'fault_type':'3ph','rf_ohm':0,'location_pct':50}, "
            "{'type':'clear','target':'Bus 7','time':0.2}, {'type':'switch','target':'Line 5-7','time':0.2,'action':'open'}, "
            "{'type':'param','target':'G1','time':1,'variable':'pgini','value':80}, "
            "{'type':'load','target':'Load A','time':1,'dP_pct':10,'dQ_pct':0}, {'type':'outage','target':'G2','time':1}"))] = None,
        monitor: Annotated[dict[str, list[str]] | list[str] | str | None, Field(description=(
            "'auto' (speed, angle, P and Q of machines + u1 of buses), {'G*.ElmSym':['s:xspeed'],'Bus 5':['m:u1']}, "
            "['Bus 5:m:u1', ...] or null (uses the study case ElmRes)"))] = "auto",
        network: Annotated[Literal["balanced", "unbalanced"], Field()] = "balanced",
        t_start: float | None = None,
        options: Annotated[dict[str, Any] | None, Field(description="extra ComInc attributes")] = None,
        plot: bool = True, export: bool = True,
        variables_report: Annotated[list[str] | None, Field(description="'Obj:var' filters for statistics, CSV and plot")] = None) -> str:
    """RMS or EMT dynamic simulation (ComInc + ComSim). Returns per-variable statistics, CSV, PNG and
    indicators: angle spread, frequency nadir, minimum and final voltages."""
    return _call("run_dynamic_simulation", {"sim_type": sim_type, "t_end": t_end, "step_s": step_s,
                                            "events": events, "monitor": monitor, "network": network,
                                            "t_start": t_start, "options": options, "plot": plot,
                                            "export": export, "variables_report": variables_report}, long=True)


@tool()
def list_protection(pattern: Annotated[str, Field(description="device filter")] = "*.ElmRelay,*.RelFuse",
                    include_settings: bool = True, limit: int = 60) -> str:
    """Protection devices (relays and fuses): location, type, CT, stages and main settings."""
    return _call("list_protection", {"pattern": pattern, "include_settings": include_settings, "limit": limit},
                 timeout=600)


@tool(EDIT)
def protection_study(fault_locations: Annotated[list[str] | str, Field(description="buses where the fault is applied")],
                     fault_type: Annotated[str, Field(description="3ph | 2ph | 1ph | 2phg")] = "3ph",
                     method: str = "iec60909",
                     devices: Annotated[str, Field(description="pattern of the devices to report")] = "*.ElmRelay,*.RelFuse",
                     only_tripping: bool = True,
                     plot_devices: Annotated[list[str] | None, Field(description="relays for the time-overcurrent curve (PNG)")] = None) -> str:
    """Protection tripping times for faults at the given buses (ComShc with protection taken into account;
    c:Ttrip of the stages) and, optionally, the time-overcurrent curve of the selected relays
    (own PNG in primary A: IEC 60255, IEEE C37.112 and definite time, with the fault currents marked)."""
    return _call("protection_study", {"fault_locations": fault_locations, "fault_type": fault_type,
                                      "method": method, "devices": devices, "only_tripping": only_tripping,
                                      "plot_devices": plot_devices}, timeout=1200)


@tool(EDIT)
def run_modal_analysis(damping_threshold_pct: Annotated[float, Field(description="threshold for flagging poorly damped modes")] = 5.0,
                       participation: bool = True, max_modes: int = 60,
                       options: dict[str, Any] | None = None, export: str | bool | None = None) -> str:
    """Modal / small-signal analysis (ComMod): eigenvalues, frequency, damping, unstable or
    poorly damped modes and, optionally, the states with the highest participation."""
    return _call("run_modal_analysis", {"damping_threshold_pct": damping_threshold_pct,
                                        "participation": participation, "max_modes": max_modes,
                                        "options": options, "export": export}, long=True)


@tool(EDIT)
def run_quasi_dynamic(start: Annotated[str | None, Field(description="ISO start 'YYYY-MM-DD HH:MM'")] = None,
                      end: Annotated[str | None, Field(description="ISO end")] = None,
                      step: float = 1.0,
                      step_unit: Annotated[Literal["seconds", "minutes", "hours", "days"], Field()] = "hours",
                      method: Annotated[Literal["ac", "dc"], Field()] = "ac",
                      variables: Annotated[list[str] | None, Field(description="'Obj:var' result filters")] = None,
                      options: dict[str, Any] | None = None, plot: bool = True, export: bool = True) -> str:
    """Quasi-dynamic simulation (ComStatsim): series of load flows with load and generation profiles."""
    return _call("run_quasi_dynamic", {"start": start, "end": end, "step": step, "step_unit": step_unit,
                                       "method": method, "variables": variables, "options": options,
                                       "plot": plot, "export": export}, long=True)


@tool(EDIT)
def run_harmonic_analysis(analysis: Annotated[Literal["harmonic_load_flow", "frequency_sweep"], Field()] = "harmonic_load_flow",
                          buses: Annotated[list[str] | str | None, Field(description="buses to report or plot")] = None,
                          f_start: float = 10.0, f_stop: float = 2500.0, f_step: float = 5.0,
                          options: dict[str, Any] | None = None, plot: bool = True,
                          export: str | bool | None = None) -> str:
    """Harmonics: harmonic load flow (ComHldf: THD and individual harmonics per bus) or frequency sweep
    (ComFsweep: Thévenin impedance vs. frequency, resonances)."""
    return _call("run_harmonic_analysis", {"analysis": analysis, "buses": buses, "f_start": f_start,
                                           "f_stop": f_stop, "f_step": f_step, "options": options, "plot": plot,
                                           "export": export}, long=True)


@tool(EDIT)
def run_pv_curve(loads: Annotated[list[str] | str | None, Field(description="loads to scale (all by default)")] = None,
                 buses: Annotated[list[str] | str | None, Field(description="buses to record (all by default)")] = None,
                 options: dict[str, Any] | None = None, plot: bool = True, export: bool = True) -> str:
    """P-V curves (ComPvcurves): scales the load up to collapse; gives the loading margin and the critical bus."""
    return _call("run_pv_curve", {"loads": loads, "buses": buses, "options": options, "plot": plot,
                                  "export": export}, long=True)


@tool(EDIT)
def run_command(class_name: Annotated[str, Field(description="case command: ComOpf, ComRel3, ComArcreport, ComMot, ComHostcap, ComRed, ComAmpacity, ComStatsim...")],
                settings: Annotated[dict[str, Any] | None, Field(description="attributes set only during the run (describe_class)")] = None,
                result: Annotated[str | None, Field(description="ElmRes to read afterwards")] = None,
                variables: list[str] | None = None, plot: bool = False, export: bool = False) -> str:
    """Runs any calculation command of the active study case with temporary settings. Reports the return code,
    the messages and the ElmRes that changed, and optionally reads one of them."""
    return _call("run_command", {"class_name": class_name, "settings": settings, "result": result,
                                 "variables": variables, "plot": plot, "export": export}, long=True)


# ==================================================================== results
@tool()
def list_results() -> str:
    """ElmRes of the active study case (name, calculation type, columns, rows)."""
    return _call("list_results", {})


@tool()
def read_results(result: Annotated[str | None, Field(description="ElmRes name; empty = the last one produced by a tool")] = None,
                 variables: Annotated[list[str] | None, Field(description="'Object:variable' filters with wildcards, e.g. ['G*:s:xspeed','Bus 5:m:u1']")] = None,
                 export: Annotated[str | bool | None, Field(description="true or a .csv path")] = None,
                 plot: Annotated[bool | str, Field(description="true or a .png path")] = False,
                 list_only: bool = False) -> str:
    """Reads an ElmRes (simulation, contingencies, quasi-dynamic...): available columns, statistics
    (min, max, initial, final), CSV and PNG plot."""
    return _call("read_results", {"result": result, "variables": variables, "export": export, "plot": plot,
                                  "list_only": list_only}, timeout=600)


@tool()
def export_diagram(diagram: Annotated[str | None, Field(description="single-line diagram (IntGrfnet) or plot page; empty = first one")] = None,
                   format: Annotated[Literal["png", "pdf", "svg", "emf", "wmf"], Field()] = "png",
                   path: str | None = None) -> str:
    """Exports a single-line diagram or a plot page with the current results. The PNG can be read with Read.
    The first export of the session takes longer, because it initializes the graphical interface."""
    return _call("export_diagram", {"diagram": diagram, "format": format, "path": path}, timeout=600)


# ===================================================================== escape
@tool(DESTRUCTIVE)
def run_python(code: Annotated[str, Field(description="Python code; persistent namespace with app, pf, ctx, find(), get(), attr(), oname(), out_path(), write_table(). Assign `result` to return data")],
               timeout_s: Annotated[float, Field(description="time limit; on expiry the engine restarts")] = 300,
               background: Annotated[bool, Field(description="returns a job_id if it takes longer than ~90 s")] = False,
               reset: bool = False) -> str:
    """Runs Python code inside the PowerFactory engine: DPL/DSL via ComDpl, creating or modifying any
    object, studies not covered by the typed tools. Do not use dialogs (ShowModal*, ShowEditDialog):
    they block the engine."""
    return _call("run_python", {"code": code, "reset": reset or None}, timeout=timeout_s, long=background)


def main() -> None:
    # stdout is the MCP channel: any accidental print goes to stderr.
    sys.stdout.reconfigure(encoding="utf-8")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
