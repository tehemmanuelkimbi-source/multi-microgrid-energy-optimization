"""Three interconnected microgrids: 24-hour MILP scheduling in PuLP.

Notation follows the course material:
  fL = electrical load; fR = renewable generation; Bp/Sp = buy/sell price;
  fG = grid power; fNR = non-renewable (fossil) generation;
  fS_C/fS_D = storage charge/discharge power; x = state of charge.
Positive f12 denotes MG1 -> MG2 and positive f23 denotes MG2 -> MG3.

PART 5 implements one initial Pareto front and two sensitivity analyses:
  1. Normalised economic-cost vs CO2-emissions Pareto front.
  2. Cost vs temperature-tracking-error sensitivity to alpha_T.
  3. Cost vs CO2-emissions sensitivity to alpha_E.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pulp
import matplotlib.pyplot as plt


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_FILE = PROJECT_ROOT / "data" / "data_m.xlsx"
# Repository figures are kept in images/, separate from the tabular workbook
# in results/.
RESULTS_DIR = PROJECT_ROOT / "images"
OPTIMAL = pulp.LpStatusOptimal
SENSITIVITY_ALPHAS = (0.1, 0.2, 0.3, 0.4, 0.5, 1.0, 2.5, 5.0, 10.0, 50.0, 100.0, 500.0)


# ============================================================================
# PART 1 - DATA
# Goal: read every time series and collect all parameters in one dictionary.
# No later part of the program reads the Excel workbook directly.
# ============================================================================
def read_microgrid_sheet(path, sheet_name, has_temperature=False):
    """Read the 24 hourly rows of one worksheet into course notation."""
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
    raw = raw.dropna(how="all", axis=0).dropna(how="all", axis=1)
    hour = pd.to_numeric(raw.iloc[:, 0], errors="coerce")
    table = raw[hour.between(1, 24)].reset_index(drop=True)
    if len(table) != 24:
        raise ValueError(f"{sheet_name}: expected 24 hourly rows, found {len(table)}")

    data = {
        "fL": table.iloc[:, 1].astype(float).to_numpy(),  # electrical load [kW]
        "fR": table.iloc[:, 2].astype(float).to_numpy(),  # renewable generation [kW]
        "Bp": table.iloc[:, 3].astype(float).to_numpy(),  # grid purchase price [EUR/kWh]
        "Sp": table.iloc[:, 4].astype(float).to_numpy(),  # grid sale price [EUR/kWh]
    }
    if has_temperature:
        data["Text"] = table.iloc[:, 5].astype(float).to_numpy() - 273.15  # outdoor [degC]
    return data


def load_data(path=DATA_FILE, scenario="capacity_upgrade"):
    """Load input data and transparent scenario assumptions.

    diagnostic: original 200-kW grid/line limits, with ENS only to measure
                the physical shortage. It is not a final operating solution.
    capacity_upgrade: feasible planning scenario: 300-kW grids, 500-kW lines.
    """
    mg1_excel = read_microgrid_sheet(path, "Microgrid 1")
    mg2_excel = read_microgrid_sheet(path, "microgrid 2", has_temperature=True)
    mg3_excel = read_microgrid_sheet(path, "Microgrid 3")
    if scenario not in {"diagnostic", "capacity_upgrade"}:
        raise ValueError("scenario must be 'diagnostic' or 'capacity_upgrade'")

    # A 24-hour dispatch has negligible standing loss: beta = 1.0.
    stationary_storage = {
        "beta": 1.0, "fSmax": 40.0, "xmin": 0.10, "xmax": 0.90,
        "eta_ch": 0.85, "eta_disch_factor": 1.15,
    }
    data = {
        # MG1: PV + stationary storage + fossil generator + external grid.
        "MG1": {**mg1_excel, **stationary_storage, "fGmax": 200.0,
                "fNRmax": 120.0, "CNR": 0.10, "CAPsto": 200.0, "xin": 0.30},
        # MG2: wind + one EV (V2G/G2V) + heat pump; no external grid.
        "MG2": {**mg2_excel,
                "people": np.array([0] * 7 + [25] * 6 + [0] + [25] * 2 + [0] * 8, dtype=float),
                "CAP_EV": 500.0, "xEVin": 0.20, "xEVmin": 0.10, "xEVmax": 0.90,
                "xEVdeadline": 0.80, "EV_departure": 18,
                "fEVmax": 50.0, "beta_EV": 1.0,
                "eta_ch_EV": 0.85, "eta_disch_EV_factor": 1.15,
                "CB": 50.0, "Rext": 400.0, "EER_HP": 1.8, "fHPmax": 50.0,
                "Qint_person": 0.10, "T0": 20.0, "Tset": 21.0,
                # Comfort band reverted to the agreed +/-2 degC around 21 degC.
                "Tmin": 19.0, "Tmax": 23.0},
        # MG3: renewable generation + stationary storage + external grid.
        # The project brief does not specify an MG3 fossil generator.
        "MG3": {**mg3_excel, **stationary_storage, "fGmax": 200.0,
                "CAPsto": 800.0, "xin": 0.20},
        "SYS": {"dt": 1.0, "T": 24, "fG_SYSmax": 1000.0,
                "C_CO2": 0.030, "eNR": 0.3706, "eGrid": 0.1752,
                "f12max": 200.0, "f23max": 200.0,
                "VoLL": 10.0, "thetaT": 100.0,
                "allow_ENS": scenario == "diagnostic"},
    }
    if scenario == "capacity_upgrade":
        data["MG1"]["fGmax"] = data["MG3"]["fGmax"] = 300.0
        data["SYS"]["f12max"] = data["SYS"]["f23max"] = 500.0
    return data


# ============================================================================
# PART 2 - DECISION VARIABLES
# Goal: create all hourly power-flow, SOC, thermal, and binary variables.
# Every list indexed by t has 24 values; x and T have 25 values because they
# include the state before hour 1 and after hour 24.
# ============================================================================
def create_variables(data):
    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    hours = range(sys["T"])
    v = {}

    # MG1 and MG3 have import/export fG and stationary storage fS.
    for label, mg in (("1", mg1), ("3", mg3)):
        v[f"fG{label}_in"] = [pulp.LpVariable(f"fG{label}_in_{t}", 0, mg["fGmax"]) for t in hours]
        v[f"fG{label}_out"] = [pulp.LpVariable(f"fG{label}_out_{t}", 0, mg["fGmax"]) for t in hours]
        v[f"fS{label}_C"] = [pulp.LpVariable(f"fS{label}_C_{t}", 0, mg["fSmax"]) for t in hours]
        v[f"fS{label}_D"] = [pulp.LpVariable(f"fS{label}_D_{t}", 0, mg["fSmax"]) for t in hours]
        v[f"uS{label}"] = [pulp.LpVariable(f"uS{label}_{t}", cat="Binary") for t in hours]
        v[f"x{label}"] = [pulp.LpVariable(f"x{label}_{t}", mg["xmin"], mg["xmax"])
                          for t in range(sys["T"] + 1)]

    v["fNR1"] = [pulp.LpVariable(f"fNR1_{t}", 0, mg1["fNRmax"]) for t in hours]
    v["fEV_C"] = [pulp.LpVariable(f"fEV_C_{t}", 0, mg2["fEVmax"]) for t in hours]
    v["fEV_D"] = [pulp.LpVariable(f"fEV_D_{t}", 0, mg2["fEVmax"]) for t in hours]
    v["uEV"] = [pulp.LpVariable(f"uEV_{t}", cat="Binary") for t in hours]
    v["xEV"] = [pulp.LpVariable(f"xEV_{t}", mg2["xEVmin"], mg2["xEVmax"])
                for t in range(sys["T"] + 1)]
    v["fHP"] = [pulp.LpVariable(f"fHP_{t}", 0, mg2["fHPmax"]) for t in hours]
    v["Tb"] = [pulp.LpVariable(f"Tb_{t}", 5, 35) for t in range(sys["T"] + 1)]
    v["Tdev"] = [pulp.LpVariable(f"Tdev_{t}", 0) for t in hours]

    # Signed line flows: f12 > 0 is MG1 -> MG2, f23 > 0 is MG2 -> MG3.
    v["f12"] = [pulp.LpVariable(f"f12_{t}", -sys["f12max"], sys["f12max"]) for t in hours]
    v["f23"] = [pulp.LpVariable(f"f23_{t}", -sys["f23max"], sys["f23max"]) for t in hours]
    # ENS is a diagnostic slack only; it is fixed to zero in the planning case.
    v["ENS1"] = [pulp.LpVariable(f"ENS1_{t}", 0) for t in hours]
    v["ENS2"] = [pulp.LpVariable(f"ENS2_{t}", 0) for t in hours]
    v["ENS3"] = [pulp.LpVariable(f"ENS3_{t}", 0) for t in hours]
    return v


# ============================================================================
# PART 3 - CONSTRAINTS
# Goal: impose hourly power balances, mutual exclusion, SOC dynamics, EV
# availability/deadline, the building-temperature model, and grid limits.
# ============================================================================
def build_constraints(problem, v, data, temperature_mode="bounds"):
    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    hours, dt = range(sys["T"]), sys["dt"]

    for t in hours:
        # Electrical balances: supply + ENS = load + charging + exports.
        problem += (v["fG1_in"][t] - v["fG1_out"][t] + v["fNR1"][t] + mg1["fR"][t]
                    + v["fS1_D"][t] - v["fS1_C"][t] + v["ENS1"][t]
                    == mg1["fL"][t] + v["f12"][t]), f"MG1_balance_{t}"
        problem += (mg2["fR"][t] + v["fEV_D"][t] - v["fEV_C"][t] + v["f12"][t] + v["ENS2"][t]
                    == mg2["fL"][t] + v["fHP"][t] + v["f23"][t]), f"MG2_balance_{t}"
        problem += (v["fG3_in"][t] - v["fG3_out"][t] + mg3["fR"][t]
                    + v["fS3_D"][t] - v["fS3_C"][t] + v["f23"][t] + v["ENS3"][t]
                    == mg3["fL"][t]), f"MG3_balance_{t}"

        if not sys["allow_ENS"]:
            for label in ("1", "2", "3"):
                problem += v[f"ENS{label}"][t] == 0, f"ENS_not_allowed_{label}_{t}"

        # Big-M mutual-exclusion constraints: a battery cannot charge and discharge together.
        for label, mg in (("1", mg1), ("3", mg3)):
            problem += v[f"fS{label}_D"][t] <= mg["fSmax"] * v[f"uS{label}"][t]
            problem += v[f"fS{label}_C"][t] <= mg["fSmax"] * (1 - v[f"uS{label}"][t])
        problem += v["fEV_D"][t] <= mg2["fEVmax"] * v["uEV"][t]
        problem += v["fEV_C"][t] <= mg2["fEVmax"] * (1 - v["uEV"][t])
        problem += v["fG1_in"][t] + v["fG3_in"][t] <= sys["fG_SYSmax"], f"system_grid_limit_{t}"

    # Stationary-storage SOC equations and terminal SOC conditions.
    for label, mg in (("1", mg1), ("3", mg3)):
        problem += v[f"x{label}"][0] == mg["xin"], f"x{label}_initial"
        for t in hours:
            problem += (v[f"x{label}"][t + 1] == mg["beta"] * v[f"x{label}"][t]
                        + dt * (mg["eta_ch"] * v[f"fS{label}_C"][t]
                                - mg["eta_disch_factor"] * v[f"fS{label}_D"][t]) / mg["CAPsto"]), f"x{label}_state_{t}"
        problem += v[f"x{label}"][-1] >= mg["xin"], f"x{label}_terminal"

    # EV SOC and 18:00 departure requirement.
    problem += v["xEV"][0] == mg2["xEVin"], "xEV_initial"
    for t in hours:
        problem += (v["xEV"][t + 1] == mg2["beta_EV"] * v["xEV"][t]
                    + dt * (mg2["eta_ch_EV"] * v["fEV_C"][t]
                            - mg2["eta_disch_EV_factor"] * v["fEV_D"][t]) / mg2["CAP_EV"]), f"xEV_state_{t}"
    problem += v["xEV"][mg2["EV_departure"]] >= mg2["xEVdeadline"], "EV_deadline"
    for t in range(mg2["EV_departure"], sys["T"]):
        problem += v["fEV_C"][t] == 0, f"EV_absent_charge_{t}"
        problem += v["fEV_D"][t] == 0, f"EV_absent_discharge_{t}"

    # Building thermal model: Tb(t+1) = Tb(t) + dt/CB * (Q_HP + Q_ext + Q_int).
    problem += v["Tb"][0] == mg2["T0"], "Tb_initial"
    for t in hours:
        q_ext = (mg2["Text"][t] - v["Tb"][t]) / mg2["Rext"]
        q_int = mg2["Qint_person"] * mg2["people"][t]
        q_hp = mg2["EER_HP"] * v["fHP"][t]
        problem += v["Tb"][t + 1] == v["Tb"][t] + dt / mg2["CB"] * (q_hp + q_ext + q_int), f"Tb_state_{t}"
        if temperature_mode == "bounds":
            # Phase B: hard comfort band 19-23 degC.
            problem += v["Tb"][t + 1] >= mg2["Tmin"], f"Tb_min_{t}"
            problem += v["Tb"][t + 1] <= mg2["Tmax"], f"Tb_max_{t}"
        elif temperature_mode == "tracking":
            # Phase A: |Tb - Tset| linearised through Tdev (penalised in objective).
            problem += v["Tdev"][t] >= v["Tb"][t + 1] - mg2["Tset"], f"Tdev_pos_{t}"
            problem += v["Tdev"][t] >= mg2["Tset"] - v["Tb"][t + 1], f"Tdev_neg_{t}"
        else:
            raise ValueError("temperature_mode must be 'bounds' or 'tracking'")


# ============================================================================
# PART 4 - OBJECTIVE FUNCTIONS AND SOLUTION
# Goal: calculate cost, emissions and ENS; select one objective; solve with CBC.
# The Pareto curve uses a normalised weighted sum of economic cost and emissions.
# ============================================================================
def build_expressions(v, data):
    """Build named performance expressions; they are evaluated after solving."""
    mg1, mg3, sys = data["MG1"], data["MG3"], data["SYS"]
    hours, dt = range(sys["T"]), sys["dt"]
    energy_cost = pulp.lpSum((mg1["Bp"][t] * v["fG1_in"][t] - mg1["Sp"][t] * v["fG1_out"][t]
                              + mg3["Bp"][t] * v["fG3_in"][t] - mg3["Sp"][t] * v["fG3_out"][t]
                              + mg1["CNR"] * v["fNR1"][t]) * dt for t in hours)
    emissions = pulp.lpSum((sys["eNR"] * v["fNR1"][t]
                             + sys["eGrid"] * (v["fG1_in"][t] + v["fG3_in"][t])) * dt for t in hours)
    carbon_cost = sys["C_CO2"] * emissions
    tracking_error = pulp.lpSum(v["Tdev"])
    tracking_cost = sys["thetaT"] * tracking_error
    ens_kwh = dt * pulp.lpSum(v["ENS1"] + v["ENS2"] + v["ENS3"])
    return {"energy_cost": energy_cost, "emissions_kg": emissions,
            "carbon_cost": carbon_cost, "economic_cost": energy_cost + carbon_cost,
            "tracking_error_degC_h": tracking_error, "tracking_cost": tracking_cost, "ENS_kWh": ens_kwh}


def solve_model(data, temperature_mode="bounds", objective="cost", alpha=1.0, scales=None):
    """Assemble Parts 2-4 and return results only when CBC finds an optimum."""
    problem = pulp.LpProblem("Three_Microgrid_Scheduling", pulp.LpMinimize)
    v = create_variables(data)
    build_constraints(problem, v, data, temperature_mode)
    e = build_expressions(v, data)
    if objective == "cost":
        objective_expression = e["economic_cost"]
    elif objective == "cost_with_tracking":
        # Phase-A cost: energy + carbon + comfort-tracking penalty.
        objective_expression = e["economic_cost"] + e["tracking_cost"]
    elif objective == "emissions":
        objective_expression = e["emissions_kg"]
    elif objective == "tracking":
        objective_expression = e["tracking_cost"]
    elif objective == "sensitivity_tracking":
        # J1 = J_total + alpha_T * J_track; alpha_T has units EUR/(degC h).
        objective_expression = e["economic_cost"] + alpha * e["tracking_error_degC_h"]
    elif objective == "sensitivity_emissions":
        # J2 = J_total + alpha_E * E_CO2; alpha_E has units EUR/kg CO2.
        # J_total already includes the base carbon cost C_CO2 * E_CO2.
        objective_expression = e["economic_cost"] + alpha * e["emissions_kg"]
    elif objective == "pareto_cost_emissions":
        if scales is None:
            raise ValueError("Pareto optimisation requires normalisation scales")
        # In tracking mode the cost side of the Pareto includes the tracking
        # penalty, exactly as the project statement requires
        # ("costs including those of emissions, tracking").
        cost_part = e["economic_cost"] + (e["tracking_cost"]
                                          if temperature_mode == "tracking" else 0.0)
        objective_expression = (alpha * cost_part / scales["cost"]
                                + (1 - alpha) * e["emissions_kg"] / scales["emissions"])
    elif objective == "pareto_cost_tracking":
        if scales is None:
            raise ValueError("Pareto optimisation requires normalisation scales")
        # L1 tracking error keeps the formulation as an MILP for CBC:
        # J_track = sum_t |Tb(t) - Tset|, represented by Tdev.
        objective_expression = (alpha * e["economic_cost"] / scales["cost"]
                                + (1 - alpha) * e["tracking_cost"] / scales["tracking"])
    else:
        raise ValueError("unknown objective")
    # ENS has a deliberately dominant penalty and is available only in diagnostic mode.
    problem += objective_expression + data["SYS"]["VoLL"] * e["ENS_kWh"]
    problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=120))
    status = pulp.LpStatus[problem.status]
    if problem.status != OPTIMAL:
        return {"status": status, "message": "No dispatch is shown: the model is not optimal."}

    def val(expression):
        number = pulp.value(expression)
        return 0.0 if number is None else float(number)

    hours = range(data["SYS"]["T"])
    result = {"status": status, "objective": objective, "alpha": alpha,
              **{name: val(expression) for name, expression in e.items()}}
    result.update({
        "fG1": [val(v["fG1_in"][t]) - val(v["fG1_out"][t]) for t in hours],
        "fNR1": [val(v["fNR1"][t]) for t in hours],
        "fS1": [val(v["fS1_D"][t]) - val(v["fS1_C"][t]) for t in hours], "x1": [val(x) for x in v["x1"]],
        "fEV": [val(v["fEV_D"][t]) - val(v["fEV_C"][t]) for t in hours], "xEV": [val(x) for x in v["xEV"]],
        "fHP": [val(v["fHP"][t]) for t in hours], "Tb": [val(x) for x in v["Tb"]],
        "fG3": [val(v["fG3_in"][t]) - val(v["fG3_out"][t]) for t in hours],
        "fS3": [val(v["fS3_D"][t]) - val(v["fS3_C"][t]) for t in hours], "x3": [val(x) for x in v["x3"]],
        "f12": [val(v["f12"][t]) for t in hours], "f23": [val(v["f23"][t]) for t in hours],
        "ENS": [val(v["ENS1"][t]) + val(v["ENS2"][t]) + val(v["ENS3"][t]) for t in hours],
    })
    # A tracking variable is not part of an emissions-only objective (alpha=0),
    # so its solver value need not be the exact absolute temperature deviation.
    # Calculate the reported tracking cost directly from the physical solution.
    # This is therefore valid for every Pareto point and for both comfort modes.
    result["tracking_cost_model"] = result["tracking_cost"]
    result["tracking_cost"] = data["SYS"]["thetaT"] * sum(
        abs(result["Tb"][t + 1] - data["MG2"]["Tset"])
        for t in hours
    )
    result["tracking_error_degC_h"] = result["tracking_cost"] / data["SYS"]["thetaT"]
    return result


# ============================================================================
# PART 5 - THE TWO PARETO CURVES (weighted sum, 11 points each)
# ============================================================================
def pareto_cost_emissions(data, temperature_mode="bounds", n_points=11):
    """Solve the normalised weighted-sum cost-vs-emissions Pareto curve.

    temperature_mode="tracking" -> Curve 1: costs INCLUDE the tracking penalty.
    temperature_mode="bounds"   -> Curve 2: hard temperature bounds 19-23 degC.
    """
    cost_objective = "cost_with_tracking" if temperature_mode == "tracking" else "cost"
    cost_optimum = solve_model(data, temperature_mode, objective=cost_objective)
    emissions_optimum = solve_model(data, temperature_mode, objective="emissions")
    if cost_optimum["status"] != "Optimal" or emissions_optimum["status"] != "Optimal":
        raise RuntimeError("A feasible scenario is required for Pareto analysis.")

    include_tracking = temperature_mode == "tracking"
    anchor_cost = (cost_optimum["economic_cost"]
                   + (cost_optimum["tracking_cost"] if include_tracking else 0.0))
    scales = {"cost": max(anchor_cost, 1.0),
              "emissions": max(emissions_optimum["emissions_kg"], 1.0)}

    points = []
    for alpha in np.linspace(0, 1, n_points):
        r = solve_model(data, temperature_mode, objective="pareto_cost_emissions",
                        alpha=float(alpha), scales=scales)
        if r["status"] != "Optimal":
            raise RuntimeError(f"Pareto point alpha={alpha:.2f} did not solve to optimality.")
        # Report the cost the project asks for on the y-axis of each curve.
        r["cost_for_pareto"] = (r["economic_cost"]
                                + (r["tracking_cost"] if include_tracking else 0.0))
        points.append(r)
    return points


def sensitivity_analysis(data, kind, alpha_values=SENSITIVITY_ALPHAS):
    """Solve one coefficient sensitivity analysis using the requested alphas.

    `kind='tracking'` solves J_total + alpha_T * J_track with tracking mode.
    `kind='emissions'` solves J_total + alpha_E * E_CO2 with 19-23 degC bounds.
    These coefficients are not Pareto shares and may therefore be greater than 1.
    """
    if kind == "tracking":
        temperature_mode, objective = "tracking", "sensitivity_tracking"
    elif kind == "emissions":
        temperature_mode, objective = "bounds", "sensitivity_emissions"
    else:
        raise ValueError("kind must be 'tracking' or 'emissions'")
    points = []
    for coefficient in alpha_values:
        r = solve_model(data, temperature_mode=temperature_mode, objective=objective, alpha=coefficient)
        if r["status"] != "Optimal":
            raise RuntimeError(f"Sensitivity point alpha={coefficient} did not solve to optimality.")
        r["sensitivity_alpha"] = coefficient
        points.append(r)
    return points


def print_pareto(points, title):
    """Print one Pareto curve as a compact table."""
    print(f"\n--- {title} ---")
    print(" alpha   cost [EUR]   emissions [kg CO2]   tracking [EUR]   status")
    for p in points:
        print(f"{p['alpha']:6.2f}   {p['cost_for_pareto']:10.2f}   {p['emissions_kg']:18.1f}"
              f"   {p['tracking_cost']:14.2f}   {p['status']}")


def print_tracking_sensitivity(points):
    """Print J_total vs J_track sensitivity results."""
    print("\n--- SENSITIVITY 1: total cost vs temperature-tracking error ---")
    print(" alpha_T   economic cost [EUR]   tracking error [degC h]   status")
    for p in points:
        print(f"{p['sensitivity_alpha']:7.2f}   {p['economic_cost']:19.2f}"
              f"   {p['tracking_error_degC_h']:23.3f}   {p['status']}")


def print_emissions_sensitivity(points):
    """Print J_total vs physical CO2-emissions sensitivity results."""
    print("\n--- SENSITIVITY 2: total cost vs CO2 emissions ---")
    print(" alpha_E   economic cost [EUR]   CO2 emissions [kg/day]   status")
    for p in points:
        print(f"{p['sensitivity_alpha']:7.2f}   {p['economic_cost']:19.2f}"
              f"   {p['emissions_kg']:22.1f}   {p['status']}")


def plot_cost_emissions_pareto(points, title, filename):
    """Save one cost-emissions Pareto figure."""
    RESULTS_DIR.mkdir(exist_ok=True)
    plt.figure(figsize=(9, 6))
    plt.plot([p["emissions_kg"] for p in points],
             [p["cost_for_pareto"] for p in points], "-o", markersize=5)
    plt.xlabel("CO2 emissions [kg]")
    plt.ylabel("Total cost [EUR]")
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    path = RESULTS_DIR / filename
    plt.savefig(path, dpi=200)
    plt.close()
    print(f"Pareto figure saved to {path}")


def plot_tracking_sensitivity(points, filename="sensitivity_1_cost_vs_tracking.png"):
    """Save the cost-vs-temperature-tracking-error sensitivity figure."""
    RESULTS_DIR.mkdir(exist_ok=True)
    plt.figure(figsize=(9, 6))
    plt.plot([p["tracking_error_degC_h"] for p in points],
             [p["economic_cost"] for p in points], "-o", markersize=5)
    plt.xlabel("Absolute temperature-tracking error [degC h]")
    plt.ylabel("Economic cost (energy + carbon) [EUR]")
    plt.title("Sensitivity 1: total cost vs temperature-tracking error")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    path = RESULTS_DIR / filename
    plt.savefig(path, dpi=200)
    plt.close()
    print(f"Sensitivity figure saved to {path}")


def plot_emissions_sensitivity(points, filename="sensitivity_2_cost_vs_emissions.png"):
    """Save the cost-vs-physical-CO2-emissions sensitivity figure."""
    RESULTS_DIR.mkdir(exist_ok=True)
    plt.figure(figsize=(9, 6))
    plt.plot([p["emissions_kg"] for p in points],
             [p["economic_cost"] for p in points], "-o", markersize=5)
    plt.xlabel("Physical CO2 emissions [kg CO2/day]")
    plt.ylabel("Economic cost (energy + carbon) [EUR/day]")
    plt.title("Sensitivity 2: total cost vs CO2 emissions")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    path = RESULTS_DIR / filename
    plt.savefig(path, dpi=200)
    plt.close()
    print(f"Sensitivity figure saved to {path}")


# ============================================================================
# PART 6 - POWER-BALANCE VISUALISATION
# Goal: display the solved hourly supply/demand balance of each microgrid.
# Positive bars are supply; negative bars are local demand, charging, and export.
# ============================================================================
def _stacked_bars(hours, components, positive, ax):
    """Draw stacked bars of components that are all non-negative magnitudes."""
    bottom = np.zeros(len(hours))
    for label, values, colour in components:
        signed = values if positive else -values
        ax.bar(hours, signed, bottom=bottom if positive else -bottom,
               width=0.78, label=label, color=colour)
        bottom += values


def plot_power_balance(result, data, microgrid, filename):
    """Save one stacked hourly power-balance chart in the project notation."""
    if result["status"] != "Optimal":
        raise ValueError("Power balances can only be plotted for an optimal dispatch.")
    hours = np.arange(1, data["SYS"]["T"] + 1)
    z = np.zeros(data["SYS"]["T"])
    f12, f23 = np.asarray(result["f12"]), np.asarray(result["f23"])
    if microgrid == "MG1":
        positive = [("fR", data["MG1"]["fR"], "#5B9A68"), ("fG import", np.maximum(result["fG1"], 0), "#F4A261"),
                    ("fNR", np.asarray(result["fNR1"]), "#B8744F"), ("fS discharge", np.maximum(result["fS1"], 0), "#7CB342"),
                    ("MG2 to MG1", np.maximum(-f12, 0), "#455A64")]
        negative = [("fL", data["MG1"]["fL"], "#4F81A2"), ("fG export", np.maximum(-np.asarray(result["fG1"]), 0), "#56B4E9"),
                    ("fS charge", np.maximum(-np.asarray(result["fS1"]), 0), "#B65BAA"), ("MG1 to MG2", np.maximum(f12, 0), "#3D6B43")]
        title = "POWER BALANCE OF MICROGRID 1"
    elif microgrid == "MG2":
        positive = [("fR", data["MG2"]["fR"], "#5B9A68"), ("EV discharge", np.maximum(result["fEV"], 0), "#7CB342"),
                    ("MG1 to MG2", np.maximum(f12, 0), "#455A64"), ("MG3 to MG2", np.maximum(-f23, 0), "#546E7A")]
        negative = [("fL", data["MG2"]["fL"], "#4F81A2"), ("EV charge", np.maximum(-np.asarray(result["fEV"]), 0), "#B65BAA"),
                    ("heat pump", np.asarray(result["fHP"]), "#F4A261"), ("MG2 to MG1", np.maximum(-f12, 0), "#3D6B43"),
                    ("MG2 to MG3", np.maximum(f23, 0), "#8064A2")]
        title = "POWER BALANCE OF MICROGRID 2"
    elif microgrid == "MG3":
        positive = [("fR", data["MG3"]["fR"], "#5B9A68"), ("fG import", np.maximum(result["fG3"], 0), "#F4A261"),
                    ("fS discharge", np.maximum(result["fS3"], 0), "#7CB342"), ("MG2 to MG3", np.maximum(f23, 0), "#455A64")]
        negative = [("fL", data["MG3"]["fL"], "#4F81A2"), ("fG export", np.maximum(-np.asarray(result["fG3"]), 0), "#56B4E9"),
                    ("fS charge", np.maximum(-np.asarray(result["fS3"]), 0), "#B65BAA"), ("MG3 to MG2", np.maximum(-f23, 0), "#3D6B43")]
        title = "POWER BALANCE OF MICROGRID 3"
    else:
        raise ValueError("microgrid must be 'MG1', 'MG2', or 'MG3'")

    fig, ax = plt.subplots(figsize=(11, 7))
    _stacked_bars(hours, positive, True, ax)
    _stacked_bars(hours, negative, False, ax)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set(title=title, xlabel="TIME [h]", ylabel="POWER [kW]")
    ax.set_xticks(hours)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.12), frameon=False)
    fig.tight_layout()
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / filename
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Power-balance figure saved to {path}")


def print_results(result, title):
    """Print one valid dispatch in the same notation used by the model."""
    print(f"\n--- {title} ---\nStatus: {result['status']}")
    if result["status"] != "Optimal":
        print(result["message"])
        return
    print(f"Economic cost: EUR {result['economic_cost']:.2f} | CO2: {result['emissions_kg']:.1f} kg | ENS: {result['ENS_kWh']:.2f} kWh")
    print(" h  fNR1    fG1    fS1    x1    fEV   xEV   fHP    Tb    fG3    fS3    x3    f12    f23    ENS")
    for t in range(24):
        print(f"{t + 1:2d} {result['fNR1'][t]:6.1f} {result['fG1'][t]:6.1f} {result['fS1'][t]:6.1f} {result['x1'][t]:5.2f} "
              f"{result['fEV'][t]:6.1f} {result['xEV'][t]:5.2f} {result['fHP'][t]:5.1f} {result['Tb'][t + 1]:5.2f} "
              f"{result['fG3'][t]:6.1f} {result['fS3'][t]:6.1f} {result['x3'][t]:5.2f} {result['f12'][t]:6.1f} {result['f23'][t]:6.1f} {result['ENS'][t]:6.1f}")


if __name__ == "__main__":
    print("Input file:", DATA_FILE)
    print("Notation: fL load, fR renewables, fG grid, fNR fossil, fS storage, x SOC.")
    print_results(solve_model(load_data(scenario="diagnostic"), objective="cost"),
                  "Diagnostic: original 200-kW grid and line limits")
    planning_data = load_data(scenario="capacity_upgrade")
    planning_result = solve_model(planning_data, objective="cost")
    print_results(planning_result, "Feasible planning scenario: 300-kW grids and 500-kW lines")

    # PART 6 - visualise hourly power balances.
    print("\n--- POWER-BALANCE FIGURES ---")
    plot_power_balance(planning_result, planning_data, "MG1", "power_balance_MG1.png")
    plot_power_balance(planning_result, planning_data, "MG2", "power_balance_MG2.png")
    plot_power_balance(planning_result, planning_data, "MG3", "power_balance_MG3.png")

    # PART 5 - initial Pareto front, then the two requested coefficient sensitivities.
    pareto_tracking = pareto_cost_emissions(planning_data, temperature_mode="tracking")
    tracking_sensitivity = sensitivity_analysis(planning_data, "tracking")
    emissions_sensitivity = sensitivity_analysis(planning_data, "emissions")
    print_pareto(pareto_tracking, "INITIAL PARETO: costs (incl. emissions + tracking) vs emissions")
    print_tracking_sensitivity(tracking_sensitivity)
    print_emissions_sensitivity(emissions_sensitivity)
    print("\n--- PARETO AND SENSITIVITY FIGURES ---")
    plot_cost_emissions_pareto(pareto_tracking,
                               "Cost (carbon + tracking) vs CO2 emissions",
                               "pareto_1_initial_cost_vs_emissions.png")
    plot_tracking_sensitivity(tracking_sensitivity)
    plot_emissions_sensitivity(emissions_sensitivity)
