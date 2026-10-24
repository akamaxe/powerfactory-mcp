# powerfactory-mcp

An [MCP](https://modelcontextprotocol.io) server for using **DIgSILENT PowerFactory** from
[Claude Code](https://docs.anthropic.com/en/docs/claude-code). It lets you ask Claude, in plain language, to
analyze networks, edit models and run studies:

- load flow, N-1 and P-V curves;
- short circuit (IEC 60909, VDE, ANSI, complete method);
- RMS/EMT simulation with custom events;
- protection: tripping times and coordination curves;
- modal analysis, harmonics and quasi-dynamic simulation.

For everything else, it runs Python code inside PowerFactory.

> Independent project, not affiliated with DIgSILENT GmbH. PowerFactory is a trademark of DIgSILENT and you need
> your own license. Tested with **PowerFactory 2024 SP2** and Python 3.12 on Windows 11.

## Requirements

| | |
|---|---|
| OS | Windows 10/11 |
| PowerFactory | With the Python 3.12 API (PF 2024 ships `Python\3.8` to `Python\3.12`). The license must allow Python access. `show_gui` and `export_diagram` need a full license, not an engine-only one. |
| Python | 3.12: from [python.org](https://www.python.org/downloads/) (with the `py` launcher) or from Anaconda/Miniconda. |
| Claude Code | Installed and on the `PATH` (`claude --version`). |
| Git | To clone and update. |

## Installation

```powershell
git clone https://github.com/akamaxe/powerfactory-mcp.git
cd powerfactory-mcp
py -3.12 install.py
```

With Anaconda or Miniconda, instead of the last line:

```powershell
conda create -n powerfactory-mcp python=3.12 -y
conda activate powerfactory-mcp
python install.py --no-venv
```

Then open Claude Code (or type `/mcp` if it was already open) and check that `powerfactory` shows as connected.
Try something like: *"Connect in parallel mode, list my projects and tell me which one has the most buses."*

### What `install.py` does

1. Finds the most recent PowerFactory installation that ships the API for your Python (or the one in `--pf-dir`).
2. Creates `.venv` inside the repository and installs the package in editable mode, so `git pull` updates the code.
3. Creates the **workspace**, `%USERPROFILE%\Documents\PowerFactory` by default, with `projects`, `results`,
   `backups`, `scripts`, `logs` and `_ref`. It also writes `powerfactory-mcp.toml` with your paths; if one already
   exists, it is not overwritten.
4. Generates `_ref\` from your installation:
   - the attribute catalog for every class, extracted from `datascheme.db`;
   - the text of the Python, DPL and error code references, so Claude can search them.
   DIgSILENT's documentation is not included in the repository: it is generated on each machine.
5. Copies the `powerfactory` skill to `~\.claude\skills`, with the verified enumerations, the result variables
   and the API pitfalls. It also places a `CLAUDE.md` in the workspace.
6. Checks that the package and the `powerfactory` module import.
7. Registers the server in Claude Code: `claude mcp add -s user powerfactory ...`.

| Option | Purpose |
|---|---|
| `--dry-run` | Show the steps without doing anything |
| `--pf-dir "C:\...\PowerFactory 2024"` | Use a specific installation |
| `--workspace D:\Studies\PF` | Use another workspace |
| `--no-venv` | Install into the active environment (e.g. a conda env) |
| `--force` | Replace the skill (with a backup in `backups\`) and re-register the server |
| `--skip-install`, `--skip-ref`, `--skip-skill`, `--skip-register` | Skip steps |

### Recommended permissions

So that Claude Code does not ask for confirmation on every tool, but still does before running code or deleting
objects, add this to `~\.claude\settings.json`:

```json
"permissions": {
  "allow": ["mcp__powerfactory"],
  "ask": ["mcp__powerfactory__run_python", "mcp__powerfactory__delete_objects"]
}
```

## How it works

```
Claude Code ──stdio──▶ MCP server (powerfactory_mcp.server)
                           │  named pipe + authkey (multiprocessing.connection)
                           ▼
                       worker process (powerfactory_mcp.worker)
                           └─ powerfactory.GetApplicationExt(...)  ← single thread, PowerFactory engine
```

The engine lives in a separate process for several reasons:

- PowerFactory cannot be restarted within the same process.
- It allows switching between exclusive and parallel mode.
- A hung study is cut off by a timeout without bringing down the server.

The worker starts with the first call and is released after 20 min of inactivity, so you can reopen the
GUI. Long studies return a `job_id`, which you query with `jobs`. Every response includes the new errors and
warnings from PowerFactory's output window.

### Connection modes

| Mode | Database | Changes | PowerFactory GUI |
|---|---|---|---|
| `exclusive` (default) | The real one | Persist | Must be **closed** (otherwise ExitError 4002) |
| `parallel` | In-memory copy (`/inMemory`) | Lost on disconnect, unless you export a .pfd | Can stay open |

Use `parallel` for exploring and for destructive tests. Before editing in `exclusive`, ask for a backup with
`export_project`. The default mode is set in `powerfactory-mcp.toml`.

## Tools (33)

| Group | Tools |
|---|---|
| Session | `status`, `connect`, `disconnect`, `show_gui`, `output_window`, `jobs` |
| Project | `list_projects`, `open_project`, `project_overview`, `set_active`, `import_project`, `export_project` |
| Model | `find_objects`, `describe_object`, `describe_class`, `set_attributes` (returns the previous values and keeps a change log), `add_element` (creates cubicles and switches), `delete_objects` |
| Studies | `run_load_flow` (AC, unbalanced, DC), `run_contingency_analysis`, `run_short_circuit`, `run_dynamic_simulation` (RMS/EMT), `list_protection`, `protection_study`, `run_modal_analysis`, `run_quasi_dynamic`, `run_harmonic_analysis` (harmonic load flow and frequency sweep), `run_pv_curve`, `run_command` (any command: ComOpf, ComRel3, ComArcreport...) |
| Results | `list_results`, `read_results` (CSV/XLSX and PNG plots), `export_diagram` (single-line diagram to PNG) |
| Code | `run_python`: persistent namespace with `app`, `pf` and helpers |

Typed studies **restore** the study case's command settings (ComLdf, ComShc, ComInc...) when they finish.
The auxiliary objects they create carry the `MCP` prefix (`MCP N-1`, `MCP Events`, `MCP Results`...) and can be
reused or deleted. Output goes to `results\<project>\`.

### Example prompts

- *"Open project X in parallel mode, run a load flow and tell me which buses are outside 0.95–1.05 pu."*
- *"Single-phase IEC 60909 short circuit at all 220 kV buses; export it to Excel."*
- *"Simulate a three-phase fault at Bus 7 cleared at 100 ms. Is the system stable? Plot the angles."*
- *"Which relay trips first for a fault at Bus 5, and with what margin? Draw the curves."*
- *"Compute the electromechanical modes and flag the ones with less than 5% damping."*
- *"Double the load of Load A, recalculate and then put it back as it was."*

## Configuration

`powerfactory-mcp.toml` (in the workspace; Claude Code finds it through the `PF_MCP_CONFIG` variable).
After editing it, reconnect with `/mcp`.

| Section | Key | What it is |
|---|---|---|
| `[powerfactory]` | `install_dir`, `python_api` | Installation and API folder; empty = auto-detect |
| | `username`, `password` | PowerFactory user; empty = the local database's user |
| | `default_mode` | `exclusive` or `parallel` |
| | `show_gui`, `extra_args` | Show the engine window on startup; extra PowerFactory arguments |
| `[paths]` | `workspace`, `results_dir`, `backups_dir`, `logs_dir` | Working folders; missing ones go under `workspace` |
| | `allowed` | Folders where the server may read and write files |
| `[timeouts]` | `startup_s`, `default_s`, `inline_wait_s`, `job_max_s`, `idle_release_min` | Startup, normal operation, switch to `job_id`, job cap and idle release |

## Security

- `run_python` runs arbitrary code inside PowerFactory with your user's permissions. Keep it on `ask`.
- `delete_objects` requires `confirm=true`; deleted objects go to PowerFactory's recycle bin.
- In `exclusive` mode, changes stay in your real database. Back up with `export_project` or by copying the
  PowerFactory workspace folder, in `%LOCALAPPDATA%\DIgSILENT\PowerFactory 20xx\Workspace.*`.
- The server only reads and writes files inside `allowed`.

## Validation

Checked against PowerFactory 2024 SP2:

- **Load flow** (9-bus system): matches PowerFactory's balance.
- **RMS**: with a three-phase fault cleared at 180 ms the system stays stable; at 450 ms a generator loses
  synchronism.
- **Modal**: detects an unstable 3.46 Hz mode in a case with a poorly tuned PSS.
- **P-V curve**: the built-in continuation gives 643.8 MW versus 633.9 MW from ComPvcurves.
- **Short circuit**: all 6,418 buses of a national model (SEIN) in 106 s.
- **Protection**: the tripping time of an IEC very inverse relay (13.35 s) matches the hand calculation.
- **Protocol**: `tests\mcp_smoke.py` walks through the tools over stdio, just like Claude Code.

## Known limitations

- **Time-overcurrent curve**: the native one (PltOvercurrent) could not be automated. `protection_study` draws its
  own with matplotlib, with IEC 60255, IEEE C37.112 and definite-time curves; non-standard curves are skipped with
  a warning.
- **Tripping times**: they correspond to the initial fault; successive openings (ComShctrace) are not simulated.
- **First single-line diagram export**: takes 2 to 4 min per session, because it initializes the graphical
  interface; later ones take about 1 s. After that, activating large projects is slower. Export diagrams at the end.
- **Memory**: parallel mode copies the database into RAM (about 2.5 GB with a model of about 6,400 buses). With
  little RAM, use exclusive mode.
- **Interface**: the `show_gui` window is read-only.

The API pitfalls that silently give wrong results are documented in the skill
(`skill\powerfactory\SKILL.md`).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ExitError 4002` on connect | The PowerFactory GUI is open and the mode is `exclusive`: close it or use `connect(mode="parallel")` |
| `No PowerFactory installation with an API for Python 3.x found` | Install Python 3.12 or point to the folder with `--pf-dir` |
| The server shows as failed in `/mcp` | Check `<workspace>\logs\worker.log` and try the harness (below) |
| License error on startup | Check that CodeMeter, or your license server, is running and open PowerFactory manually once |
| Everything is very slow in parallel mode | Not enough RAM: close programs or use exclusive mode |
| From Git Bash, `/inMemory` turns into a path | Use `MSYS_NO_PATHCONV=1` |

Harness for testing the engine without Claude Code (parallel mode, does not touch your database):

```powershell
.venv\Scripts\python.exe -I -m powerfactory_mcp.dev --mode parallel --op status --op list_projects
```

## Development

```
install.py                       installer (standard library only)
powerfactory-mcp.example.toml    configuration template
src/powerfactory_mcp/
  server.py      MCP tools (MCPServer, stdio)
  client.py      worker management, jobs, timeouts, idle release
  worker.py      process that hosts the engine
  config.py      configuration and allowed paths
  setup_ref.py   generates <workspace>\_ref
  dev.py         harness without MCP
  ops/           operations that run inside the engine (one per tool)
skill/powerfactory/              skill for Claude Code
workspace/CLAUDE.md              workspace notes template
tests/test_offline.py            tests without PowerFactory (pytest)
tests/mcp_smoke.py               end-to-end MCP test (needs PowerFactory)
```

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -I tests\mcp_smoke.py --project "My project" --case "Base case" --bus "Bus 5" --rms
```

A new operation is defined with `@op("name")` in `ops\` and exposed with a `@tool(...)` function in
`server.py`. `run_python` is handy for prototyping it first.

## Updating and uninstalling

- **Update**: run `git pull` and then `/mcp` → reconnect. If the dependencies changed, also run
  `.venv\Scripts\python.exe -m pip install -e .`. To update the skill:
  `py -3.12 install.py --skip-install --skip-ref --force`.
- **Uninstall**:
  1. Run `claude mcp remove powerfactory -s user`.
  2. Delete `~\.claude\skills\powerfactory` and the repository folder, which includes `.venv`.

  The workspace and your PowerFactory database are left untouched.
