# Multi-Microgrid Energy Optimisation

This repository contains a reproducible 24-hour scheduling study for three interconnected microgrids.  The system includes renewable generation, stationary batteries, a fossil generator, an EV with V2G/G2V operation, a heat pump, building thermal dynamics, grid exchange, and two inter-microgrid lines.

## Solver implementations

| Script | Solver | Tracking formulation | Recommended use |
|---|---|---|---|
| `src/main.py` | PuLP + CBC | MILP-compatible absolute deviation, `sum(abs(Tb - Tset))` | Open-source/reproducible comparison model |
| `src/main_pyomo.py` | Pyomo + Gurobi | Exact quadratic term, `sum((Tb - Tset)^2)` | Authoritative project results |

The models share the same network, data, operational constraints, diagnostic scenario, feasible-planning scenario, two project-defined Pareto curves, and sensitivity analyses.  They can return different hourly flows when multiple dispatches have the same optimal objective value.

## Project-defined Pareto analyses

1. **Pareto 1:** minimise the trade-off between physical CO2 emissions and cost including energy, base carbon cost, and soft temperature tracking.
2. **Pareto 2:** remove soft tracking, impose the 19--23 degC hard temperature bounds, and minimise the cost--emissions trade-off.

## Repository layout

```text
data/                 Course input workbook
docs/                 Project brief, assumptions, and documentation
src/                  PuLP/CBC and Pyomo/Gurobi optimisation scripts
outputs/pulp_cbc/     Current PuLP figures and Excel workbook
outputs/pyomo_gurobi/ Current Pyomo figures and Excel workbook
outputs/legacy_pulp/  Previously generated material retained for traceability
legacy-lingo/         Reference-only legacy material; not used as model input
```

## Run

From `src/`:

```bash
uv run python main.py
uv run python main_pyomo.py
```

The Pyomo version requires a working Gurobi licence. Both scripts read `data/data_m.xlsx` and write their results to their respective `outputs/` folder.

## Model governance

All values not directly supplied by `data_m.xlsx` or the project brief are listed in [docs/assumption_register.md](docs/assumption_register.md). In particular, the virtual comfort cost and VoLL are scenario assumptions, not market tariffs.
