# Optimisation source code

- `main.py`: PuLP/CBC MILP implementation. It uses an L1 absolute-temperature-deviation proxy because CBC does not solve MIQP models.
- `main_pyomo.py`: Pyomo/Gurobi MIQP implementation. It retains the exact squared temperature-tracking term and is the primary implementation for final analysis.

Both scripts resolve the workbook and output paths from the repository root, so they should be run from this directory or any working directory.

```bash
uv run python main.py
uv run python main_pyomo.py
```
