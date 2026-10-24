# Common result variables (PF 2024, verified live unless noted)

## Load flow
| Object | Variable | Meaning |
|---|---|---|
| ElmTerm | `m:u` / `m:u1` | voltage [p.u.] (positive sequence) |
| ElmTerm | `m:Ul` / `m:U` | line-to-line / line-to-ground voltage [kV] |
| ElmTerm | `m:phiu` | angle [°] |
| ElmTerm | `m:u:A`, `m:u:B`, `m:u:C` | per-phase voltages (unbalanced LF) |
| ElmTerm | `m:Pgen`, `m:Pload` | connected generation / load [MW] |
| ElmLne | `c:loading` | loading [%] |
| ElmLne | `m:P:bus1`, `m:Q:bus1`, `m:I:bus1` | flow at terminal i [MW, Mvar, kA] (`bus2` = terminal j) |
| ElmLne | `c:Ploss`, `c:Qloss`, `c:Losses` | losses [MW, Mvar, kW] |
| ElmTr2 | `c:loading`, `m:P:bushv`, `m:Q:bushv`, `m:I:bushv`, `nntap` | loading, HV-side flows, tap |
| ElmSym | `m:P:bus1`, `m:Q:bus1`, `m:u1:bus1` | resulting dispatch |
| Summary Grid (`app.GetSummaryGrid()`) | `c:GenP`, `c:GenQ`, `c:LoadP`, `c:LoadQ`, `c:LossP`, `c:LossQ` | system balance |

## Short circuit (after ComShc)
| Object | Variable | Meaning |
|---|---|---|
| ElmTerm | `m:Ikss`, `m:Skss` | initial current / power [kA, MVA] |
| ElmTerm | `m:ip`, `m:Ib`, `m:Ith` | peak, breaking and thermal [kA] |
| ElmTerm | `m:R`, `m:X`, `m:Z` | short-circuit impedance [Ω] |
| ElmTerm | `m:Ikss:A/B/C`, `m:I0x3` | per-phase currents and 3·I0 (asymmetrical faults) |
| Branches | `m:Ikss:bus1`, `m:Skss:bus1`, `m:ip:bus1`, `m:Ith:bus1` | contributions per terminal |

## Contingencies (ComSimoutage → ElmRes calTp=13)
- One row per contingency (`b:number` = −1 is the base case) and columns `c:loading` (branches) and `m:u` (buses).
- `1e+35` = not recorded: it is outside the recording limits (`SetLimits`).
- After the calculation, each branch exposes `c:maxLoading` (the maximum loading across contingencies).

## RMS/EMT simulation
| Object | Variable | Meaning |
|---|---|---|
| ElmSym | `s:xspeed` | speed [p.u.] |
| ElmSym | `c:firel` | rotor angle relative to the reference [°] (0 on the reference machine) |
| ElmSym | `s:P1`, `s:Q1`, `s:pt`, `s:ut`, `s:ve`, `s:ie` | electrical P and Q, turbine power, terminal voltage, excitation voltage and current |
| ElmSym | `c:dfrot`, `c:fipol` | frequency deviation, pole angle |
| ElmTerm | `m:u1`, `m:fe` | voltage [p.u.], electrical frequency [Hz] (`m:fe` unverified) |
| ElmRes | `b:tnow` | time [s] (default column) |

## Protection (after ComShc with iopt_prot=1)
| Object | Variable | Meaning |
|---|---|---|
| RelToc / RelIoc / RelDis* | `c:Ttrip` | per-phase tripping time [s] (list; 9999.999 = no trip) |
| RelToc / RelIoc | `c:Itrip` | per-phase tripping current |
| RelLogic | `c:itrip` | 1 if the logic issues a trip command |
| RelToc settings | `Ipset`, `Tpset`, `pcharac` (TypChatoc), `idir` | pickup, dial, curve, direction |
| RelIoc settings | `Ipset`, `Tset` | pickup and delay |

## Modal (ComMod → ElmRes calTp=5)
- One row per eigenvalue; the default column is `b:eigno`.
- `b:eigvalr` = σ [1/s] and `b:eigvali` = ω [rad/s]. Hence f = ω/2π and ζ = −σ/√(σ²+ω²).
- Per state of each DSL model or machine: `"<state>,c:p_mag"` (participation), `"<state>,c:lEVec_mag"`
  (controllability) and `"<state>,c:rEVec_mag"` (observability), with their `_phi`.

## Quasi-dynamic (ComStatsim → ElmRes calTp=29)
- `b:ucttime` axis (Unix time).
- Recorded by default: `c:umin`, `c:umax`, `c:Maxloading` and `c:LoadP` of the grid; `c:loading` of branches;
  `m:u` of buses; `m:P:bus1` of generators.
