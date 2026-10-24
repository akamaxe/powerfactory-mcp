# run_python recipes (PowerFactory 2024)

The namespace contains `app`, `pf`, `ctx`, `find`, `get`, `attr`, `oname`, `out_path` and `write_table`.
It persists between calls (`reset=true` clears it). To return data, assign `result`.

## Iterate over objects and read results
```python
rows = []
for t in find("*.ElmTerm"):                      # calculation-relevant objects
    rows.append([t.loc_name, attr(t, "uknom"), attr(t, "m:u")])
result = rows
```

## Run a command with temporary settings (and restore them)
```python
ldf = app.GetFromStudyCase("ComLdf")
old = ldf.GetAttribute("iopt_at")
ldf.SetAttribute("iopt_at", 1)                   # automatic taps
try:
    ret = ldf.Execute()
finally:
    ldf.SetAttribute("iopt_at", old)
result = {"ret": ret, "valid": app.IsLdfValid()}
```

## Run an existing DPL script
```python
dpl = get("My Script.ComDpl")                    # or find("*.ComDpl", "project")
dpl.SetInputParameterDouble("x", 1.5)            # if the script has input parameters
result = dpl.Execute()
```

## Create a simulation event by hand
```python
case = app.GetActiveStudyCase()
evf = app.GetFromStudyCase("ComInc").GetAttribute("p_event")      # IntEvt folder of the study case
ev = evf.CreateObject("EvtShc", "fault Bus 7")
ev.SetAttribute("p_target", get("Bus 7.ElmTerm")); ev.SetAttribute("time", 0.1); ev.SetAttribute("i_shc", 0)
```
(`run_dynamic_simulation` does this in its own folder without touching the study case's.)

## Read a full ElmRes column (fast)
```python
res = get("My results.ElmRes"); res.Load()
col = res.FindColumn(get("G1.ElmSym"), "s:xspeed")
vec = ctx.user().GetContents("_mcp_tmp.IntFolder")[0].GetContents("pfmcp_vec.IntVec")[0]
res.GetColumnValues(vec, col); y = list(vec.GetAttribute("V"))
res.GetColumnValues(vec, -1);  t = list(vec.GetAttribute("V"))   # axis (time)
res.Release()
```

## Global library types
```python
lib = app.GetGlobalLibrary("TypRelay")           # relay type folder in the global library
types = lib.GetContents("*overcurrent*.TypRelay", 1)
```

## Change a relay's type and regenerate its stages
```python
r = get("R1.ElmRelay"); r.SetAttribute("typ_id", types[0]); r.SlotUpdate()
```

## Custom messages in the output window
```python
app.PrintInfo("hello"); app.PrintWarn("careful"); app.PrintError("failed")
```

## What NOT to do
- `ShowModalBrowser`, `ShowModalSelectBrowser`, `ShowEditDialog` and `AddContingencies()` open dialogs and block the engine.
- `del app` or calling `GetApplication()` again: PowerFactory cannot restart within the same process.
- `GetContents(..., 1)` on the user or a huge folder: it is extremely slow. Narrow the search to the project or a grid.
