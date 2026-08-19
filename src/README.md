# Python optimisation model

`main.py` contains the 24-hour three-microgrid MILP model implemented with
PuLP/CBC. It reads `../data/data_m.xlsx` and writes PNG figures to `../images/`.

The script is organised into data, decision variables, constraints, objectives,
power-balance visualisation, initial Pareto analysis, and two coefficient
sensitivity analyses.
