"""Session: engine status, licence, visible window and output window."""

from __future__ import annotations

from powerfactory_mcp.ops import OpError, op

LICENCE_MODULES = {
    "script": "Scripting and Automation",
    "contingency": "Contingency Analysis",
    "prot": "Time-Overcurrent Protection",
    "dist": "Distance Protection",
    "stab": "Stability Analysis (RMS)",
    "stabemt": "Electromagnetic Transients (EMT)",
    "smallsig": "Small Signal Stability (eigenvalues)",
    "qdynsim": "Quasi-Dynamic Simulation",
    "harm": "Power Quality and Harmonic Analysis",
    "opfact": "OPF (Economic Dispatch)",
    "opfreact": "OPF (Reactive Power Optimisation)",
    "reli": "Reliability Analysis",
    "arcflash": "Arc-Flash Analysis",
    "motstart": "Motor Starting",
    "netred": "Network Reduction",
    "distr": "Distribution Network Tools",
    "transmission": "Transmission Network Tools",
    "staest": "State Estimation",
    "probAnalysis": "Probabilistic Analysis",
    "unitcomm": "Unit Commitment",
}


@op()
def status(ctx) -> dict:
    app, pf = ctx.app, ctx.pf
    d = {
        "powerfactory": pf.__version__,
        "mode": ctx.mode,
        "user": ctx.user().loc_name,
        "workspace": app.GetWorkspaceDirectory(),
        "language": app.GetLanguage(),
        "gui_visible": ctx.gui_visible,
        **ctx.brief(),
    }
    lic = {}
    for key, label in LICENCE_MODULES.items():
        try:
            lic[key] = bool(app.LicenceHasModule(key))
        except Exception:  # noqa: BLE001
            lic[key] = None
    d["licence"] = {"included": [k for k, v in lic.items() if v], "missing": [k for k, v in lic.items() if v is False]}
    if app.GetActiveStudyCase() is not None:
        d["valid_results"] = {
            "load_flow": bool(app.IsLdfValid()),
            "short_circuit": bool(app.IsShcValid()),
            "rms_initialised": bool(app.IsRmsValid()),
            "simulation": bool(app.IsSimValid()),
        }
    return d


@op()
def show_gui(ctx, visible: bool = True) -> dict:
    try:
        if visible:
            ctx.gui_initialized = True
            ctx.app.Show()
        else:
            ctx.app.Hide()
    except Exception as e:  # noqa: BLE001
        raise OpError(f"Could not {'show' if visible else 'hide'} the window: {e} "
                      "(Show() requires a full licence, not an engine licence).") from e
    ctx.gui_visible = bool(visible)
    return {"visible": ctx.gui_visible,
            "note": "The engine window is read-only ([NON-INTERACTIVE MODE]); it mirrors what Claude does."}


@op()
def output_window(ctx, filter: str = "all", last_n: int = 100, contains: str | None = None) -> dict:
    """Output window history (operations clear the window; it accumulates here)."""
    lines = list(ctx.history)
    f = filter.lower()
    if f.startswith("err"):
        lines = [x for x in lines if _kind(x) == "error"]
    elif f.startswith("warn"):
        lines = [x for x in lines if _kind(x) in ("error", "warning")]
    if contains:
        lines = [x for x in lines if contains.lower() in x.lower()]
    return {"total": len(lines), "lines": lines[-last_n:]}


def _kind(line: str) -> str:
    low = line.lower()
    if "error" in low[:40] or "/err" in low[:20]:
        return "error"
    if "warn" in low[:40] or "/wrng" in low[:20]:
        return "warning"
    return "info"
