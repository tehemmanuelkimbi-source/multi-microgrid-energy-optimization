# Assumption Register

This register lists values that are not time-series inputs in `data/data_m.xlsx`. The project brief remains the authority for project scope and supplied thermal parameters.

| Parameter | Value used | Status / rationale |
|---|---:|---|
| Time step | 1 h | Hourly input data and 24-hour horizon. |
| MG1/MG3 grid capacity | 200 kW diagnostic; 300 kW planning | Diagnostic case exposes bottlenecks; planning case is the feasible capacity-upgrade scenario. |
| Line capacities | 200 kW diagnostic; 500 kW planning | Scenario definition used to demonstrate congestion and a feasible network. |
| Stationary BESS | MG1: 200 kWh; MG3: 800 kWh; 40 kW power | Model input/assumption set used by both Python versions. |
| Storage and EV efficiencies | charging 0.85; discharge-loss factor 1.15 | Adopted round-trip-loss representation. |
| Fossil marginal cost, `CNR` | 0.10 EUR/kWh | Explicit economic modelling assumption. |
| Carbon price | 0.030 EUR/kgCO2 | Equivalent to 30 EUR/tCO2, specified in the project brief. |
| Fossil/grid emission factors | 0.3706 / 0.1752 kgCO2/kWh | Italian electricity-emission assumptions, documented in the project analysis. |
| VoLL | 10 EUR/kWh | Reliability penalty used only when ENS is enabled in the diagnostic case; planning and Pareto cases enforce ENS = 0. |
| Initial/target/bound temperature | 20 / 21 / 19--23 degC | Project thermal setpoint plus selected hard-bound case. |
| Thermal model | `CB=50 kWh/degC`, `Rext=400 degC/kW`, `EER=1.8` | Supplied by the project brief. |
| Occupancy | 25 people, 08:00--13:00 and 14:00--16:00 | Interpreted as occupied intervals 08:00--13:00 and 14:00--16:00, with a 13:00--14:00 break. Internal gain is 0.1 kW/person. |
| Renewable curtailment | Allowed | Maintains physical feasibility when local load, storage, and export cannot absorb available RES. |
| `thetaT` | 100 EUR/(degC² h) in Pyomo; virtual L1 analogue in PuLP | Virtual comfort-cost assumption for Pareto 1; it expresses a high comfort priority and is not a market tariff. A quadratic virtual-discomfort formulation is supported by [González-Briones et al. (2024)](https://www.mdpi.com/3029380), which notes that the coefficient reflects decision-maker preference. |
| Pareto method | Normalized weighted sum, alpha = 0, 0.1, ..., 1 | Lecturer-confirmed approach. Pareto 1 includes tracking; Pareto 2 replaces tracking with hard temperature bounds. |

## Solver scope

The Pyomo/Gurobi implementation is a convex MIQP and uses the exact term:

\[
J^{track}=\sum_{t=0}^{23}(T_t^B-T^{set})^2.
\]

The PuLP/CBC model is an MILP and therefore uses an L1 proxy, \(\sum_t|T_t^B-T^{set}|\). It is retained as an open-source cross-check, not as a numerically identical quadratic model.

## Legacy material

`legacy-lingo/` is retained only for historical reference. Neither Python model reads it or uses it to set current project values.
