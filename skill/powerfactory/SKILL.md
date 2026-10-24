---
name: powerfactory
description: >
  Use when working with DIgSILENT PowerFactory (2024) through the `powerfactory` MCP server: opening projects,
  load flow, contingency (N-1), short circuit (IEC 60909/ANSI/complete), RMS/EMT dynamic simulation, protection
  (relays, tripping times, time-overcurrent curves), modal/small-signal analysis, harmonics, quasi-dynamic
  simulation, P-V curves, editing network data, or writing PowerFactory Python/DPL code for run_python. Covers
  attribute names, result variables, verified enumerations and the traps that silently give wrong results.
  Use it whenever the user mentions PowerFactory, DIgSILENT, DPL, DSL, .pfd projects or power system studies.
---

# PowerFactory 2024 via MCP

Server `powerfactory`. The engine runs in a worker process. Everything lives under the **workspace**
(`<workspace>`, `Documents\PowerFactory` by default; `status` shows the path): the `powerfactory-mcp.toml`
configuration, the conventions in `CLAUDE.md` and the `results\`, `backups\` and `_ref\` folders.

## Before you start
- **Mode.** `exclusive` (default) works on the real database: changes persist and the user's GUI
  must be closed (otherwise ExitError 4002). `parallel` works on an in-memory copy that coexists with the
  GUI; everything is lost on disconnect, except what is exported with `export_project`.
  **To explore or run destructive tests, use `connect(mode="parallel")`.**
- **Before editing in exclusive mode**, make a backup with `export_project()`, which goes to `<workspace>\backups\`.
- The engine takes about 20 s to start (about 45 s in parallel mode) and is released after 20 min of inactivity.
  `disconnect()` releases it immediately so the user can open their GUI.

## Typical workflow
1. `list_projects` → `open_project(project, study_case)` → `project_overview`.
2. Typed study:
   - `run_load_flow` (with `report="full"` / `export=true` for the tables)
   - `run_contingency_analysis`
   - `run_short_circuit`
   - `run_dynamic_simulation`
   - `list_protection` / `protection_study`
   - `run_modal_analysis`
   - `run_quasi_dynamic`
   - `run_harmonic_analysis`
   - `run_pv_curve`
3. Review:
   - `read_results` for any ElmRes.
   - `export_diagram` (PNG of the single-line diagram with results).
   - PNGs and CSVs land in `results\<project>\`, and the PNGs **can be read with Read** to view them.
4. Editing:
   - `find_objects` / `describe_object` → `set_attributes` (with `dry_run` first for bulk edits) → recalculate.
   - To revert, the response includes the `old` values, which are also kept in `changes.jsonl`,
     next to the worker log (`status` gives the path).
5. Anything without a tool goes through `run_python`. It is a persistent namespace with:
   - Objects: `app`, `pf` and `ctx`.
   - Lookup and reading: `find(pattern, scope)`, `get(spec)`, `attr(obj, name)` and `oname(obj)`.
   - Output: `out_path(kind, ext)` and `write_table(path, cols, rows)`.
   - Assign `result = ...` to return data.

Typed studies **restore** the settings of ComLdf, ComShc, ComInc... when they finish; restoring does not invalidate
the results. The objects they leave in the study case are reusable:
- `MCP N-1` (ComSimoutage).
- `MCP Events` (IntEvt).
- `MCP Results`, `MCP Harmonics` and `MCP Sweep` (ElmRes).
- The `\<user>\_mcp_tmp` folder (auxiliary IntVec and SetSelect objects).

## Names and attributes
- To reference an object: `'Bus 5'`, `'Line 4-5.ElmLne'` or the full path.
  - If a name matches both the element and its type (`T1.ElmTr2` / `T1.TypTr2`), the element is preferred.
  - Patterns accept wildcards and commas: `'*.ElmLne,*.ElmTr2'`.
- **Attribute prefixes:**
  - Input: `plini`, or with an explicit prefix `e:plini`.
  - Load flow and short-circuit results: `m:` (`m:u`, `m:P:bus1`).
  - Calculated: `c:` (`c:loading`, `c:Ploss`).
  - Simulation signals: `s:` (`s:xspeed`).
  - ElmRes results: `b:` (`b:tnow`).
- **Branch sides:** `bus1`/`bus2` on lines; `bushv`/`buslv` on ElmTr2; `bushv`/`busmv`/`buslv` on ElmTr3.
- **Input attributes of a class:**
  - `describe_class('ComLdf', contains='tap')` gives the description and unit live.
  - The full catalog, with names for 2,124 classes, is in `<workspace>\_ref\pf_attributes.tsv`; search it with Grep.
    If it is missing, generate it with `python -I -m powerfactory_mcp.setup_ref`.
- A result variable does not "exist" (`HasAttribute` = 0) until there are valid results from that calculation.

More variables in `references/variables.md`. `run_python` recipes in `references/recipes.md`.

## Enumerations verified in PF 2024
| Attribute | Values |
|---|---|
| ComLdf.iopt_net | 0 AC balanced · 1 AC unbalanced · 2 DC |
| ComShc.iopt_mde | 0 VDE 0102 · 1 IEC 60909 · 2 ANSI · 3 complete · 4 IEC 61363 · 5 IEC 61660 (DC) · 6 ANSI/IEEE 946 (DC) · 7 VDE 0102-10 (DC) |
| ComShc.iopt_shc | '3psc' 3ph · '2psc' 2ph · 'spgf' 1ph-E · '2pgf' 2ph-E · '3rst' 3ph unbal. — **an invalid string is stored without error and computes 3ph** |
| ComShc.iopt_allbus | 0 selection (`shcobj`, object or SetSelect) · 1 busbars and junction nodes · 2 all busbars |
| ComShc.iopt_cur | 0 max · 1 min |
| ComShc.iopt_prot | 0 none · 1 all · 2 main · 3 backup |
| EvtShc.i_shc | 0 3ph · 1 2ph · 2 1ph-E · 3 2ph-E · 4 **clear** the fault |
| EvtSwitch.i_switch | 0 open · 1 close |
| ComInc.iopt_sim / iopt_net | 'rms' / 'ins' (EMT) · 'sym' balanced / 'rst' three-phase |
| ComStatsim.calcPeriod | 0 day · 1 month · 2 year · 4 between startTime and endTime (Unix) · stepUnit 0 s, 1 min, 2 h, 3 days |

## Verified traps
- `GetContents('*.IntPrj', 1)` on the user walks **every** object of every project and takes
  about 150 s. To list projects, walk the folders without entering them (`list_projects` already does this).
- `ComWr.ExportGraphicTab` returns 1 if the window is not visible: it needs `app.Show()` → `IntGrfnet.Show()` →
  export → `app.Hide()`. The first time in a session takes 2 to 4 min, because it initializes the graphical
  interface; later ones take about 1 s. `app.GetDesktop()` is None until a graphic is opened.
- **Once the interface is initialized**, activating a large project opens and redraws its desktop. With the SEIN
  (about 6,400 buses) this goes from about 25 s to more than 10 min. The tools mitigate it with
  `SetGuiUpdateEnabled(0)` / `SetGraphicUpdate(0)` and by closing the desktop, which brings it down to about 90 s.
  If you are going to work with large projects, export diagrams at the end, or use `disconnect` + `connect` to
  start with a clean engine.
- The window shown with `Show()` is **[NON-INTERACTIVE MODE]**: it is for viewing, not editing.
- **Do not call dialogs** (`ShowModalBrowser`, `ShowEditDialog`, `AddContingencies()`): they block the engine and time out.
- Reading an ElmRes row by row with `GetValue` is very slow (9 s for 95,000 values). Use
  `ElmRes.GetColumnValues(IntVec, col)`, which takes 0.03 s; `read_results` already does this.
- A protection device's tripping time lives in its stages, not in the ElmRelay: `RelToc`/`RelIoc`/`RelDis*`.
  - They expose `c:Ttrip` (per-phase list, in s) and `c:Itrip`.
  - `9999.999` means no trip. The logic (`RelLogic`) exposes `c:itrip`.
  - Some relays are nested inside other relays: the cubicle may be two levels up.
- The native time-overcurrent plot could not be automated in PF 2024:
  - `GrpPage.GetOrInsertPlot(name, 10)` creates a PltLinebarplot; no code from 0 to 30 yields a PltOvercurrent.
  - A `PltOvercurrent` created with `CreateObject` exports empty.
  - That is why `protection_study(plot_devices=...)` draws its own curve with matplotlib, in primary A, from
    `Ipset`, `Tpset`/`Tset` and `pcharac` of the RelToc/RelIoc stages. It covers IEC 60255 (standard, very and
    extremely inverse, long-time), IEEE C37.112 and definite time.
  - Units verified against PF: `Ipset` in secondary A and `Tpset` = TMS (13.4 s computed versus 13.35 s from PF).
  - Non-standard curves are skipped with a warning.
- Large models have repeated names, such as the SEIN with duplicated substations and relays: the ambiguity
  message gives project-relative paths, which can be passed as-is as the object.
- **Memory:** parallel mode copies the database into RAM; with the SEIN, the worker uses about 2.5 GB. With little
  free RAM, use exclusive mode for large projects (with the GUI closed and a backup first).
- ComShctrace requires multiple faults to be defined (it fails with "No Short-Circuits defined"): for tripping
  times use ComShc with `iopt_prot=1`, like `protection_study` does.
- ComHldf and ComFsweep fail without an ElmRes in `p_resvar` ("No result file provided").
  - In the sweep, the `b:ifnow` axis is the **harmonic order** (f/frnom), not Hz.
  - Its `frnom` may not match the network (e.g. command at 50 Hz and network at 60 Hz). The
    tool warns about it; fix it in the project settings or with `options={'frnom': 60}`.
- ComPvcurves leaves the ElmRes empty with the default settings, even though it reports the critical demand in the
  messages. `run_pv_curve` does its own continuation with `ComLdf.scLoadFac`.
- `Delete()` sends the object to PowerFactory's recycle bin (recoverable). Touching element data invalidates the LF.
- From Git Bash, arguments starting with `/` (such as `/inMemory`) are rewritten as paths: use
  `MSYS_NO_PATHCONV=1`.

## Local documentation
- `<workspace>\_ref\PythonReference_en.txt` — Python API reference (text, searchable with Grep).
- `_ref\DplReference_en.txt`, `_ref\ErrorCodeReference_en.txt` (ExitError codes) and `_ref\pf_attributes.tsv`.
- PDF manuals: `<PowerFactory installation>\Help\`, including `UserManual_en.pdf` and
  the model technical references in `TechRef\`. They are protected: extract the text with pymupdf if needed.
