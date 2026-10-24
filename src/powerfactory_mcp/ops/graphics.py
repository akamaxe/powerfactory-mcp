"""Export of single-line diagrams and plot pages (ComWr.ExportGraphicTab)."""

from __future__ import annotations

from powerfactory_mcp.ops import OpError, op
from powerfactory_mcp.ops.common import study_command

FORMATS = ("png", "pdf", "svg", "emf", "wmf")


def close_graphics(ctx) -> None:
    """Closes the graphics desktop opened for the export: if it stays open, activating a large project
    (e.g. the SEIN) redraws all its single-line diagrams and takes minutes."""
    try:
        desk = ctx.app.GetDesktop()
        if desk is not None:
            desk.Close()
    except Exception:  # noqa: BLE001
        pass


def _find_graphic(ctx, name: str | None):
    dias = []
    f = ctx.app.GetProjectFolder("dia")
    if f is not None:
        dias = f.GetContents("*.IntGrfnet", 1)
    pages = []
    desk = ctx.app.GetDesktop()
    case = ctx.study_case()
    for holder in filter(None, [desk] + case.GetContents("*.SetDesktop")):
        pages += holder.GetContents("*.GrpPage") + holder.GetContents("*.SetVipage")
    allg = dias + pages
    if not allg:
        raise OpError("The project has no diagrams or plot pages.")
    if not name:
        return dias[0] if dias else pages[0], allg
    hits = [g for g in allg if g.loc_name == name] or [g for g in allg if name.lower() in g.loc_name.lower()]
    if not hits:
        raise OpError(f"'{name}' not found. Available: {[g.loc_name for g in allg]}")
    return hits[0], allg


@op()
def export_diagram(ctx, diagram: str | None = None, format: str = "png", path: str | None = None) -> dict:
    """Exports a single-line diagram (IntGrfnet) or plot page (GrpPage/SetVipage) with the current results."""
    fmt = format.lower().lstrip(".")
    if fmt not in FORMATS:
        raise OpError(f"Invalid format: {format} ({', '.join(FORMATS)})")
    g, allg = _find_graphic(ctx, diagram)
    dst = ctx.out_path(f"diagram_{g.loc_name}", fmt, path)
    if dst.suffix.lower() != f".{fmt}":
        dst = dst.with_suffix(f".{fmt}")
    was_visible = ctx.gui_visible
    ctx.gui_initialized = True
    try:
        if not was_visible:
            ctx.app.Show()  # without a visible window ExportGraphicTab returns 1
        g.Show()
        ret = study_command(ctx, "ComWr").ExportGraphicTab(g, str(dst))
    finally:
        if not was_visible:
            close_graphics(ctx)
            ctx.app.Hide()
    if ret != 0 or not dst.exists():
        raise OpError(f"Could not export '{g.loc_name}' (code {ret}).")
    return {"graphic": f"{g.loc_name}.{g.GetClassName()}", "file": str(dst),
            "size_kB": round(dst.stat().st_size / 1024, 1),
            "available": [f"{x.loc_name}.{x.GetClassName()}" for x in allg][:30]}
