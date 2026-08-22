"""Three Interconnected Microgrids: 24-Hour MILP Day-Ahead Scheduling in PuLP.

Mathematical Formulation & Optimization Framework:
==================================================
This module implements a day-ahead operational scheduling optimization model for
three interconnected multi-energy microgrids (MMGs) using PuLP and the COIN-OR CBC
open-source Mixed-Integer Linear Programming (MILP) solver.

Optimization Class:
  Mixed-Integer Linear Program (MILP).
  - Piecewise-linear L1 comfort tracking: J_track = sum_t |Tb(t) - T_set| via auxiliary slacks Tdev
  - Linear electrical energy balances and storage SOC state-space transitions
  - Binary variables for storage/grid bidirectional mutual exclusion

System Architecture & Notation:
--------------------------------
â€¢ MG1: Photovoltaic (PV) + Stationary BESS + Fossil Gen + Grid Connection
â€¢ MG2: Wind Turbine + EV (V2G/G2V) + Heat Pump + Building Thermal Dynamics
â€¢ MG3: Renewables (RES) + Stationary BESS + Grid Connection
â€¢ Line Interconnections: Radial topology [MG1] <--(f12)--> [MG2] <--(f23)--> [MG3]

Notation & Units:
-----------------
  fL        : Electrical load demand [kW]
  fR        : Renewable generation (PV / Wind) [kW]
  fR_C      : Renewable curtailment [kW]
  Bp, Sp    : Grid purchase and feed-in tariff prices [EUR/kWh]
  fG_in/out : Grid import / export power [kW]
  fNR       : Fossil-fueled non-renewable generation [kW]
  fS_C/fS_D : Stationary battery charge / discharge power [kW]
  fEV_C/D   : Electric vehicle charge / discharge power [kW]
  fHP       : Heat pump electrical power consumption [kW]
  Tb        : Indoor building temperature [degC]
  Tdev      : Linearized absolute temperature deviation slack |Tb - Tset| [degC]
  x, xEV    : State of Charge (SOC) fraction [0.0 to 1.0]
  f12, f23  : Inter-microgrid tie-line power flows [kW] (positive: forward flow)
  ENS       : Energy Not Served / load shedding slack [kW]
"""

from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pulp

# ============================================================================
# FILE SYSTEM & GLOBAL CONFIGURATION
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_FILE = PROJECT_ROOT / "data" / "data_m.xlsx"
RESULTS_DIR = PROJECT_ROOT / "outputs" / "pulp_cbc"
OPTIMAL = pulp.LpStatusOptimal

# Parametric sweep values for sensitivity analyses (alpha_T and alpha_E)
SENSITIVITY_ALPHAS = (0.1, 0.2, 0.3, 0.4, 0.5, 1.0, 2.5, 5.0, 10.0, 50.0, 100.0, 500.0)


# ============================================================================
# PART 1 - DATA INGESTION & SCENARIO MANAGEMENT
# Goal: Read 24-hour time series from Excel and construct clean parameter dicts.
# ============================================================================
def read_microgrid_sheet(path, sheet_name, has_temperature=False):
    """Read 24 hourly rows of time-series profiles from an Excel worksheet.

    Args:
        path (Path): Path to the Excel workbook (data_m.xlsx).
        sheet_name (str): Worksheet name ('Microgrid 1', 'microgrid 2', 'Microgrid 3').
        has_temperature (bool): Whether the sheet contains outdoor temperature data.

    Returns:
        dict[str, np.ndarray]: Dictionary containing 24-hour NumPy arrays:
            - 'fL'   : Baseline electrical load demand [kW]
            - 'fR'   : Available renewable generation [kW]
            - 'Bp'   : Grid purchase price tariff [EUR/kWh]
            - 'Sp'   : Grid feed-in sale price [EUR/kWh]
            - 'Text' : (Optional) Ambient outdoor temperature [degC] (converted from K)
    """
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
    raw = raw.dropna(how="all", axis=0).dropna(how="all", axis=1)

    # Filter rows corresponding to dispatch hours 1 through 24
    hour = pd.to_numeric(raw.iloc[:, 0], errors="coerce")
    table = raw[hour.between(1, 24)].reset_index(drop=True)
    if len(table) != 24:
        raise ValueError(f"{sheet_name}: expected 24 hourly rows, found {len(table)}")

    data = {
        "fL": table.iloc[:, 1].astype(float).to_numpy(),  # Electrical demand [kW]
        "fR": table.iloc[:, 2].astype(float).to_numpy(),  # Renewable generation [kW]
        "Bp": table.iloc[:, 3].astype(float).to_numpy(),  # Grid purchase tariff [EUR/kWh]
        "Sp": table.iloc[:, 4].astype(float).to_numpy(),  # Grid feed-in price [EUR/kWh]
    }
    if has_temperature:
        # Convert outdoor temperature from Kelvin [K] to Celsius [degC]
        data["Text"] = table.iloc[:, 5].astype(float).to_numpy() - 273.15

    return data


def load_data(path=DATA_FILE, scenario="capacity_upgrade"):
    """Load spreadsheet inputs and set transparent physical/operational assumptions.

    Scenarios:
        'diagnostic'      : Original tight line/grid limits (200 kW), allowing ENS slacks
                            to quantify physical bottlenecks and transmission congestion.
        'capacity_upgrade': Feasible planning scenario with upgraded 300 kW grid ties
                            and 500 kW tie-lines without involuntary load shedding.

    Args:
        path (Path): Path to data Excel workbook.
        scenario (str): 'diagnostic' or 'capacity_upgrade'.

    Returns:
        dict: Fully structured system parameter dictionary with sub-dicts for
              MG1, MG2, MG3, and SYS.
    """
    if scenario not in {"diagnostic", "capacity_upgrade"}:
        raise ValueError("scenario must be 'diagnostic' or 'capacity_upgrade'")

    # Common stationary battery storage parameters (BESS)
    # beta: hourly standing loss factor (1.0 = lossless self-discharge over 24h)
    # eta_ch: charging efficiency (0.85 = 85%)
    # eta_disch_factor: inverse discharge efficiency (1.15 approx 1 / 0.87)
    stationary_storage = {
        "beta": 1.0,
        "fSmax": 40.0,             # Max charge/discharge power [kW]
        "xmin": 0.10,              # Min state of charge (10%)
        "xmax": 0.90,              # Max state of charge (90%)
        "eta_ch": 0.85,            # Battery charging efficiency
        "eta_disch_factor": 1.15,  # Storage discharge loss multiplier (1 / eta_disch)
    }

    # Ingest time-series sheets for all 3 microgrids
    mg1_excel = read_microgrid_sheet(path, "Microgrid 1")
    mg2_excel = read_microgrid_sheet(path, "microgrid 2", has_temperature=True)
    mg3_excel = read_microgrid_sheet(path, "Microgrid 3")

    data = {
        # --------------------------------------------------------------------
        # MICROGRID 1: PV + Fossil Generator + Stationary Battery + Grid Tie
        # --------------------------------------------------------------------
        "MG1": {
            **mg1_excel,
            **stationary_storage,
            "fGmax": 200.0,        # Grid import/export transformer capacity [kW]
            "fNRmax": 120.0,       # Fossil generator maximum power output [kW]
            "CNR": 0.10,           # Fossil generation marginal fuel cost [EUR/kWh]
            "CAPsto": 200.0,       # Stationary battery energy capacity [kWh]
            "xin": 0.30,           # Initial & minimum terminal SOC [fraction]
        },
        # --------------------------------------------------------------------
        # MICROGRID 2: Wind + EV (V2G) + Heat Pump + Building Thermal Dynamics
        # --------------------------------------------------------------------
        "MG2": {
            **mg2_excel,
            # Occupancy profile: 25 people present during office hours (08:00-13:00, 14:00-16:00)
            "people": np.array([0] * 7 + [25] * 5 + [0] + [25] * 2 + [0] * 9, dtype=float),
            "CAP_EV": 500.0,       # Electric Vehicle battery capacity [kWh]
            "xEVin": 0.20,         # Initial EV SOC at t = 0 [fraction]
            "xEVmin": 0.10,        # Minimum EV battery SOC [fraction]
            "xEVmax": 0.90,        # Maximum EV battery SOC [fraction]
            "xEVdeadline": 0.80,   # Required minimum SOC upon departure at 18:00
            "EV_departure": 18,    # EV departure hour index (18:00)
            "fEVmax": 50.0,        # EV bidirectional inverter rating [kW]
            "beta_EV": 1.0,        # EV self-discharge factor over 24h
            "eta_ch_EV": 0.85,     # EV charging efficiency
            "eta_disch_EV_factor": 1.15,  # EV discharging loss factor (1 / eta_disch)
            # Building 1st-order lumped thermal parameter model (RC circuit)
            "CB": 50.0,            # Building effective thermal capacitance [kWh / degC]
            "Rext": 400.0,         # Envelope thermal resistance [degC / kW]
            "EER_HP": 1.8,         # Heat pump Energy Efficiency Ratio / COP [kW_thermal / kW_electric]
            "fHPmax": 50.0,        # Heat pump maximum electrical power [kW]
            "Qint_person": 0.10,   # Internal sensible heat gain per occupant [kW / person]
            "T0": 20.0,            # Initial indoor temperature at t = 0 [degC]
            "Tset": 21.0,          # Thermal comfort setpoint target [degC]
            "Tmin": 19.0,          # Minimum indoor comfort temperature bound [degC]
            "Tmax": 23.0,          # Maximum indoor comfort temperature bound [degC]
        },
        # --------------------------------------------------------------------
        # MICROGRID 3: Renewables + Stationary Battery + Grid Connection
        # --------------------------------------------------------------------
        "MG3": {
            **mg3_excel,
            **stationary_storage,
            "fGmax": 200.0,        # Grid connection capacity [kW]
            "CAPsto": 800.0,       # Large stationary battery capacity [kWh]
            "xin": 0.20,           # Initial & minimum terminal SOC [fraction]
        },
        # --------------------------------------------------------------------
        # SYSTEM-WIDE PARAMETERS & LINE TRANSMISSION LIMITS
        # --------------------------------------------------------------------
        "SYS": {
            "dt": 1.0,             # Optimization time step [hours]
            "T": 24,               # Scheduling horizon length [hours]
            "fG_SYSmax": 1000.0,   # Macrogrid total simultaneous substation import limit [kW]
            "C_CO2": 0.030,        # Base carbon tax / emission price [EUR / kg CO2]
            "eNR": 0.3706,         # Fossil generator emission intensity [kg CO2 / kWh]
            "eGrid": 0.1752,       # Macrogrid electricity emission factor [kg CO2 / kWh]
            "f12max": 200.0,       # Line capacity MG1 <-> MG2 [kW]
            "f23max": 200.0,       # Line capacity MG2 <-> MG3 [kW]
            "VoLL": 10.0,          # Value of Lost Load penalty for ENS [EUR / kWh]
            "thetaT": 100.0,       # Virtual comfort penalty weight [EUR / (degC * h)]
            "allow_ENS": scenario == "diagnostic",  # Enable ENS slacks only in diagnostic mode
        },
    }

    # Apply upgraded transmission and substation capacities for the planning scenario
    if scenario == "capacity_upgrade":
        data["MG1"]["fGmax"] = data["MG3"]["fGmax"] = 300.0
        data["SYS"]["f12max"] = data["SYS"]["f23max"] = 500.0

    return data


# ============================================================================
# PART 2 - DECISION VARIABLES
# Goal: Create all continuous flow, state-of-charge, thermal, and binary variables.
# ============================================================================
def create_variables(data):
    """Instantiate all PuLP decision variables.

    Decision Variables Created:
      â€¢ Grid:       fG1_in, fG1_out, uG1, fG3_in, fG3_out, uG3
      â€¢ Storage:    fS1_C, fS1_D, uS1, x1, fS3_C, fS3_D, uS3, x3
      â€¢ Generation: fNR1, fR1_C, fR2_C, fR3_C (curtailment slacks)
      â€¢ Demand/HP:  fEV_C, fEV_D, uEV, xEV, fHP, Tb, Tdev (L1 tracking slack)
      â€¢ Lines:      f12, f23 (signed inter-microgrid exchanges)
      â€¢ Slacks:     ENS1, ENS2, ENS3 (Energy Not Served)

    Args:
        data (dict): System parameters dictionary from load_data().

    Returns:
        dict[str, list[pulp.LpVariable]]: Dictionary of decision variable lists.
    """
    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    hours = range(sys["T"])
    v = {}

    # ------------------------------------------------------------------------
    # MICROGRID 1 & MICROGRID 3 VARIABLES (Grid, Battery, SOC)
    # ------------------------------------------------------------------------
    for label, mg in (("1", mg1), ("3", mg3)):
        v[f"fG{label}_in"] = [pulp.LpVariable(f"fG{label}_in_{t}", 0, mg["fGmax"]) for t in hours]
        v[f"fG{label}_out"] = [pulp.LpVariable(f"fG{label}_out_{t}", 0, mg["fGmax"]) for t in hours]
        v[f"uG{label}"] = [pulp.LpVariable(f"uG{label}_{t}", cat="Binary") for t in hours]
        v[f"fS{label}_C"] = [pulp.LpVariable(f"fS{label}_C_{t}", 0, mg["fSmax"]) for t in hours]
        v[f"fS{label}_D"] = [pulp.LpVariable(f"fS{label}_D_{t}", 0, mg["fSmax"]) for t in hours]
        v[f"uS{label}"] = [pulp.LpVariable(f"uS{label}_{t}", cat="Binary") for t in hours]
        v[f"x{label}"] = [pulp.LpVariable(f"x{label}_{t}", mg["xmin"], mg["xmax"])
                          for t in range(sys["T"] + 1)]

    # ------------------------------------------------------------------------
    # MICROGRID 2 VARIABLES (EV, Heat Pump, Thermal State, L1 Slack)
    # ------------------------------------------------------------------------
    v["fNR1"] = [pulp.LpVariable(f"fNR1_{t}", 0, mg1["fNRmax"]) for t in hours]
    v["fEV_C"] = [pulp.LpVariable(f"fEV_C_{t}", 0, mg2["fEVmax"]) for t in hours]
    v["fEV_D"] = [pulp.LpVariable(f"fEV_D_{t}", 0, mg2["fEVmax"]) for t in hours]
    v["uEV"] = [pulp.LpVariable(f"uEV_{t}", cat="Binary") for t in hours]
    v["xEV"] = [pulp.LpVariable(f"xEV_{t}", mg2["xEVmin"], mg2["xEVmax"])
                for t in range(sys["T"] + 1)]
    v["fHP"] = [pulp.LpVariable(f"fHP_{t}", 0, mg2["fHPmax"]) for t in hours]
    v["Tb"] = [pulp.LpVariable(f"Tb_{t}", 5, 35) for t in range(sys["T"] + 1)]
    v["Tdev"] = [pulp.LpVariable(f"Tdev_{t}", 0) for t in hours]

    # Renewable curtailment variables [kW] (prevents artificial infeasibility when RES > load+storage)
    v["fR1_C"] = [pulp.LpVariable(f"fR1_C_{t}", 0) for t in hours]
    v["fR2_C"] = [pulp.LpVariable(f"fR2_C_{t}", 0) for t in hours]
    v["fR3_C"] = [pulp.LpVariable(f"fR3_C_{t}", 0) for t in hours]

    # ------------------------------------------------------------------------
    # INTERCONNECTION LINES & DIAGNOSTIC ENERGY-NOT-SERVED (ENS)
    # ------------------------------------------------------------------------
    # Signed line flows: f12 > 0 is MG1 -> MG2; f23 > 0 is MG2 -> MG3
    v["f12"] = [pulp.LpVariable(f"f12_{t}", -sys["f12max"], sys["f12max"]) for t in hours]
    v["f23"] = [pulp.LpVariable(f"f23_{t}", -sys["f23max"], sys["f23max"]) for t in hours]

    # ENS slack variables [kW] (active only in diagnostic mode)
    v["ENS1"] = [pulp.LpVariable(f"ENS1_{t}", 0) for t in hours]
    v["ENS2"] = [pulp.LpVariable(f"ENS2_{t}", 0) for t in hours]
    v["ENS3"] = [pulp.LpVariable(f"ENS3_{t}", 0) for t in hours]

    return v


# ============================================================================
# PART 3 - PHYSICAL AND OPERATIONAL CONSTRAINTS
# Goal: Enforce nodal power balance, SOC dynamics, Big-M exclusions, thermal RC.
# ============================================================================
def build_constraints(problem, v, data, temperature_mode="bounds"):
    """Attach all physical and operational constraints to the PuLP LpProblem.

    Constraints Formulated:
      1. Nodal Power Balances (Kirchhoff's Current Law at each MG bus)
      2. Renewable Curtailment Upper Bounds (fR_C <= fR)
      3. Big-M Complementarity Mutual Exclusions (Prevent simultaneous charge/discharge & import/export)
      4. Macrogrid Substation Total Import Capacity
      5. Battery & EV State-of-Charge Linear Difference Equations & Terminal Bounds
      6. EV 18:00 Departure SOC Requirement & Post-Departure Disconnection
      7. 1st-Order Building Thermal Mass Difference Equations (RC lumped dynamics)
      8. Thermal Comfort Handling (L1 Linearized Absolute Tracking Error vs Hard Box [19, 23] degC)

    Args:
        problem (pulp.LpProblem): PuLP optimization problem instance.
        v (dict): Decision variables dictionary.
        data (dict): System parameters.
        temperature_mode (str): 'bounds' (hard 19-23 C) or 'tracking' (L1 slack Tdev).
    """
    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    hours, dt = range(sys["T"]), sys["dt"]

    for t in hours:
        # --------------------------------------------------------------------
        # 1. NODAL ELECTRICAL POWER BALANCES [kW]
        # Supply + Inflows = Demand + Storage Charging + Outflows
        # --------------------------------------------------------------------
        # MG1: Grid_in - Grid_out + Fossil + (PV - Curt) + Batt_disch - Batt_ch + ENS1 == Load + Line_12
        problem += (v["fG1_in"][t] - v["fG1_out"][t] + v["fNR1"][t] + mg1["fR"][t] - v["fR1_C"][t]
                    + v["fS1_D"][t] - v["fS1_C"][t] + v["ENS1"][t]
                    == mg1["fL"][t] + v["f12"][t]), f"MG1_balance_{t}"

        # MG2: (Wind - Curt) + EV_disch - EV_ch + Line_12 + ENS2 == Load + HeatPump + Line_23
        problem += (mg2["fR"][t] - v["fR2_C"][t] + v["fEV_D"][t] - v["fEV_C"][t] + v["f12"][t] + v["ENS2"][t]
                    == mg2["fL"][t] + v["fHP"][t] + v["f23"][t]), f"MG2_balance_{t}"

        # MG3: Grid_in - Grid_out + (RES - Curt) + Batt_disch - Batt_ch + Line_23 + ENS3 == Load
        problem += (v["fG3_in"][t] - v["fG3_out"][t] + mg3["fR"][t] - v["fR3_C"][t]
                    + v["fS3_D"][t] - v["fS3_C"][t] + v["f23"][t] + v["ENS3"][t]
                    == mg3["fL"][t]), f"MG3_balance_{t}"

        # --------------------------------------------------------------------
        # 2. RENEWABLE CURTAILMENT LIMITS [kW]
        # --------------------------------------------------------------------
        problem += v["fR1_C"][t] <= mg1["fR"][t], f"RES1_curtailment_{t}"
        problem += v["fR2_C"][t] <= mg2["fR"][t], f"RES2_curtailment_{t}"
        problem += v["fR3_C"][t] <= mg3["fR"][t], f"RES3_curtailment_{t}"

        # In planning scenarios, force Energy Not Served slacks to zero
        if not sys["allow_ENS"]:
            for label in ("1", "2", "3"):
                problem += v[f"ENS{label}"][t] == 0, f"ENS_not_allowed_{label}_{t}"

        # --------------------------------------------------------------------
        # 3. BIG-M MUTUAL EXCLUSION COMPLEMENTARITY CONSTRAINTS
        # --------------------------------------------------------------------
        for label, mg in (("1", mg1), ("3", mg3)):
            problem += v[f"fG{label}_in"][t] <= mg["fGmax"] * v[f"uG{label}"][t], f"ME_G{label}_in_{t}"
            problem += v[f"fG{label}_out"][t] <= mg["fGmax"] * (1 - v[f"uG{label}"][t]), f"ME_G{label}_out_{t}"
            problem += v[f"fS{label}_D"][t] <= mg["fSmax"] * v[f"uS{label}"][t], f"ME_S{label}_D_{t}"
            problem += v[f"fS{label}_C"][t] <= mg["fSmax"] * (1 - v[f"uS{label}"][t]), f"ME_S{label}_C_{t}"

        problem += v["fEV_D"][t] <= mg2["fEVmax"] * v["uEV"][t], f"ME_EV_D_{t}"
        problem += v["fEV_C"][t] <= mg2["fEVmax"] * (1 - v["uEV"][t]), f"ME_EV_C_{t}"

        # 4. Total Macrogrid Substation Import Limit
        problem += v["fG1_in"][t] + v["fG3_in"][t] <= sys["fG_SYSmax"], f"system_grid_limit_{t}"

    # ------------------------------------------------------------------------
    # 5. STATIONARY BATTERY STORAGE STATE-OF-CHARGE (SOC) DYNAMICS
    # x(t+1) = beta * x(t) + (dt / CAPsto) * (eta_ch * fS_C(t) - eta_disch_factor * fS_D(t))
    # ------------------------------------------------------------------------
    for label, mg in (("1", mg1), ("3", mg3)):
        problem += v[f"x{label}"][0] == mg["xin"], f"x{label}_initial"
        for t in hours:
            problem += (v[f"x{label}"][t + 1] == mg["beta"] * v[f"x{label}"][t]
                        + dt * (mg["eta_ch"] * v[f"fS{label}_C"][t]
                                - mg["eta_disch_factor"] * v[f"fS{label}_D"][t]) / mg["CAPsto"]), f"x{label}_state_{t}"
        # Terminal SOC sustainability condition (daily cyclic balance)
        problem += v[f"x{label}"][-1] >= mg["xin"], f"x{label}_terminal"

    # ------------------------------------------------------------------------
    # 6. ELECTRIC VEHICLE (EV) DYNAMICS & MOBILITY CONSTRAINTS
    # ------------------------------------------------------------------------
    problem += v["xEV"][0] == mg2["xEVin"], "xEV_initial"
    for t in hours:
        problem += (v["xEV"][t + 1] == mg2["beta_EV"] * v["xEV"][t]
                    + dt * (mg2["eta_ch_EV"] * v["fEV_C"][t]
                            - mg2["eta_disch_EV_factor"] * v["fEV_D"][t]) / mg2["CAP_EV"]), f"xEV_state_{t}"

    # Minimum SOC guarantee before departure at 18:00 (xEV >= 80%)
    problem += v["xEV"][mg2["EV_departure"]] >= mg2["xEVdeadline"], "EV_deadline"

    # Enforce zero charging / discharging when EV is away from the microgrid (t >= 18)
    for t in range(mg2["EV_departure"], sys["T"]):
        problem += v["fEV_C"][t] == 0, f"EV_absent_charge_{t}"
        problem += v["fEV_D"][t] == 0, f"EV_absent_discharge_{t}"

    # ------------------------------------------------------------------------
    # 7. BUILDING THERMAL DYNAMICS (1st-Order Lumped RC Equivalent Model)
    # Tb(t+1) = Tb(t) + (dt / CB) * [ Q_HP(t) + Q_envelope(t) + Q_internal(t) ]
    # ------------------------------------------------------------------------
    problem += v["Tb"][0] == mg2["T0"], "Tb_initial"
    for t in hours:
        q_ext = (mg2["Text"][t] - v["Tb"][t]) / mg2["Rext"]
        q_int = mg2["Qint_person"] * mg2["people"][t]
        q_hp = mg2["EER_HP"] * v["fHP"][t]
        problem += v["Tb"][t + 1] == v["Tb"][t] + dt / mg2["CB"] * (q_hp + q_ext + q_int), f"Tb_state_{t}"

        # --------------------------------------------------------------------
        # 8. THERMAL COMFORT TREATMENT
        # 'bounds'   : Strict physical hard bounds [19 degC, 23 degC]
        # 'tracking' : Linearized L1 tracking error: Tdev >= |Tb(t) - Tset|
        # --------------------------------------------------------------------
        if temperature_mode == "bounds":
            problem += v["Tb"][t + 1] >= mg2["Tmin"], f"Tb_min_{t}"
            problem += v["Tb"][t + 1] <= mg2["Tmax"], f"Tb_max_{t}"
        elif temperature_mode == "tracking":
            problem += v["Tdev"][t] >= v["Tb"][t] - mg2["Tset"], f"Tdev_pos_{t}"
            problem += v["Tdev"][t] >= mg2["Tset"] - v["Tb"][t], f"Tdev_neg_{t}"
        else:
            raise ValueError("temperature_mode must be 'bounds' or 'tracking'")


# ============================================================================
# PART 4 - OBJECTIVE FUNCTIONS, SOLUTION PIPELINE, RESULT EXTRACTION
# ============================================================================
def build_expressions(v, data):
    """Build named PuLP linear performance expressions for evaluation.

    Expressions:
      â€¢ energy_cost           : Net grid import cost minus export revenue + fossil fuel costs [EUR]
      â€¢ emissions_kg          : Physical greenhouse gas emissions from fossil gen & grid imports [kg CO2]
      â€¢ carbon_cost           : Baseline regulatory carbon cost (C_CO2 * emissions_kg) [EUR]
      â€¢ economic_cost         : Total financial expenditure (energy_cost + carbon_cost) [EUR]
      â€¢ tracking_error_degC_h : Cumulative L1 temperature-tracking error [degC * h]
      â€¢ tracking_cost         : Virtual monetary discomfort penalty (thetaT * tracking_error) [EUR]
      â€¢ ENS_kWh               : Total unserved energy volume [kWh]

    Args:
        v (dict): Decision variables dictionary.
        data (dict): System parameters.

    Returns:
        dict: Named linear combination expressions.
    """
    mg1, mg3, sys = data["MG1"], data["MG3"], data["SYS"]
    hours, dt = range(sys["T"]), sys["dt"]

    # 1. Total Daily Energy Operating Cost [EUR/day]
    energy_cost = pulp.lpSum(
        (
            mg1["Bp"][t] * v["fG1_in"][t] - mg1["Sp"][t] * v["fG1_out"][t]
            + mg3["Bp"][t] * v["fG3_in"][t] - mg3["Sp"][t] * v["fG3_out"][t]
            + mg1["CNR"] * v["fNR1"][t]
        ) * dt for t in hours
    )

    # 2. Total Daily Physical CO2 Emissions [kg CO2/day]
    emissions = pulp.lpSum(
        (
            sys["eNR"] * v["fNR1"][t]
            + sys["eGrid"] * (v["fG1_in"][t] + v["fG3_in"][t])
        ) * dt for t in hours
    )

    # 3. Base Carbon Tax Cost [EUR/day] (C_CO2 = 0.030 EUR/kg)
    carbon_cost = sys["C_CO2"] * emissions

    # 4. Linearized L1 Temperature-Tracking Error [degC * h]
    tracking_error = pulp.lpSum(v["Tdev"])

    # 5. Virtual Thermal Discomfort Cost [EUR/day]
    tracking_cost = sys["thetaT"] * tracking_error

    # 6. Unserved Energy Slack Volume [kWh/day]
    ens_kwh = dt * pulp.lpSum(v["ENS1"] + v["ENS2"] + v["ENS3"])

    return {
        "energy_cost": energy_cost,
        "emissions_kg": emissions,
        "carbon_cost": carbon_cost,
        "economic_cost": energy_cost + carbon_cost,
        "tracking_error_degC_h": tracking_error,
        "tracking_cost": tracking_cost,
        "ENS_kWh": ens_kwh,
    }


def solve_model(data, temperature_mode="bounds", objective="cost", alpha=1.0, scales=None):
    """Assemble, solve, and extract numerical results from the PuLP MILP model.

    Objectives Supported:
      â€¢ 'cost'                  : Minimize pure economic cost (energy + base carbon)
      â€¢ 'cost_with_tracking'    : Minimize economic cost + comfort tracking penalty
      â€¢ 'emissions'             : Minimize total physical CO2 emissions [kg]
      â€¢ 'tracking'              : Minimize thermal tracking penalty J_track
      â€¢ 'pareto_cost_emissions' : Normalized weighted-sum of cost and emissions:
                                  min [ alpha * (Cost/Scost) + (1-alpha) * (Emissions/Semiss) ]
      â€¢ 'sensitivity_tracking'  : Parametric sweep of comfort penalty alpha_T:
                                  min [ Cost_economic + alpha_T * tracking_error ]
      â€¢ 'sensitivity_emissions' : Parametric sweep of carbon tax alpha_E:
                                  min [ Cost_economic + alpha_E * Emissions ]

    Args:
        data (dict): System parameters dictionary.
        temperature_mode (str): 'bounds' (hard bounds) or 'tracking' (soft penalty).
        objective (str): Target objective keyword.
        alpha (float): Normalized Pareto weight [0.0 to 1.0] or sensitivity coefficient.
        scales (dict, optional): Normalization scales {'cost': ..., 'emissions': ...}.

    Returns:
        dict: Solved numerical profiles, state trajectories, and objective metrics.
    """
    problem = pulp.LpProblem("Three_Microgrid_Scheduling", pulp.LpMinimize)
    v = create_variables(data)
    build_constraints(problem, v, data, temperature_mode)
    e = build_expressions(v, data)

    # Objective Function Selection
    if objective == "cost":
        objective_expression = e["economic_cost"]
    elif objective == "cost_with_tracking":
        objective_expression = e["economic_cost"] + e["tracking_cost"]
    elif objective == "emissions":
        objective_expression = e["emissions_kg"]
    elif objective == "tracking":
        objective_expression = e["tracking_cost"]
    elif objective == "sensitivity_tracking":
        # alpha_T has physical units EUR / (degC * h)
        objective_expression = e["economic_cost"] + alpha * e["tracking_error_degC_h"]
    elif objective == "sensitivity_emissions":
        # alpha_E has physical units EUR / kg CO2 and supplements the base carbon price
        objective_expression = e["economic_cost"] + alpha * e["emissions_kg"]
    elif objective == "pareto_cost_emissions":
        if scales is None:
            raise ValueError("Pareto optimisation requires normalisation scales")
        cost_part = e["economic_cost"] + (e["tracking_cost"] if temperature_mode == "tracking" else 0.0)
        objective_expression = (alpha * cost_part / scales["cost"]
                                + (1 - alpha) * e["emissions_kg"] / scales["emissions"])
    elif objective == "pareto_cost_tracking":
        if scales is None:
            raise ValueError("Pareto optimisation requires normalisation scales")
        objective_expression = (alpha * e["economic_cost"] / scales["cost"]
                                + (1 - alpha) * e["tracking_cost"] / scales["tracking"])
    else:
        raise ValueError(f"Unknown objective: {objective}")

    # Attach objective with high VoLL penalty on Energy Not Served
    problem += objective_expression + data["SYS"]["VoLL"] * e["ENS_kWh"]
    problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=120))

    status = pulp.LpStatus[problem.status]
    if problem.status != OPTIMAL:
        return {"status": status, "message": "No dispatch is shown: the model is not optimal."}

    def val(expression):
        number = pulp.value(expression)
        return 0.0 if number is None else float(number)

    hours = range(data["SYS"]["T"])
    result = {
        "status": status,
        "objective": objective,
        "alpha": alpha,
        **{name: val(expression) for name, expression in e.items()},
    }
    result.update({
        # Net grid power: positive = import, negative = export
        "fG1": [val(v["fG1_in"][t]) - val(v["fG1_out"][t]) for t in hours],
        "fNR1": [val(v["fNR1"][t]) for t in hours],
        # Net stationary battery power: positive = discharge, negative = charge
        "fS1": [val(v["fS1_D"][t]) - val(v["fS1_C"][t]) for t in hours],
        "x1": [val(x) for x in v["x1"]],
        # Net EV power: positive = discharge (V2G), negative = charge (G2V)
        "fEV": [val(v["fEV_D"][t]) - val(v["fEV_C"][t]) for t in hours],
        "xEV": [val(x) for x in v["xEV"]],
        "fHP": [val(v["fHP"][t]) for t in hours],
        "Tb": [val(x) for x in v["Tb"]],
        "fG3": [val(v["fG3_in"][t]) - val(v["fG3_out"][t]) for t in hours],
        "fS3": [val(v["fS3_D"][t]) - val(v["fS3_C"][t]) for t in hours],
        "x3": [val(x) for x in v["x3"]],
        "fR1_curt": [val(v["fR1_C"][t]) for t in hours],
        "fR2_curt": [val(v["fR2_C"][t]) for t in hours],
        "fR3_curt": [val(v["fR3_C"][t]) for t in hours],
        "f12": [val(v["f12"][t]) for t in hours],
        "f23": [val(v["f23"][t]) for t in hours],
        "ENS": [val(v["ENS1"][t]) + val(v["ENS2"][t]) + val(v["ENS3"][t]) for t in hours],
    })

    # Calculate reported tracking metrics directly from physical state values (Tb)
    result["tracking_cost_model"] = result["tracking_cost"]
    result["tracking_cost"] = data["SYS"]["thetaT"] * sum(
        abs(result["Tb"][t] - data["MG2"]["Tset"]) for t in hours
    )
    result["tracking_error_degC_h"] = result["tracking_cost"] / data["SYS"]["thetaT"]

    return result


# ============================================================================
# PART 5 - PARETO FRONTIER & SENSITIVITY ENGINES
# ============================================================================
def pareto_cost_emissions(data, temperature_mode="bounds", n_points=11):
    """Solve the normalized weighted-sum Cost vs. CO2 Pareto curve in PuLP.

    Args:
        data (dict): System parameters.
        temperature_mode (str): 'tracking' (Pareto 1, soft) or 'bounds' (Pareto 2, hard).
        n_points (int): Number of trade-off evaluation points (default: 11).

    Returns:
        list[dict]: List of solved Pareto dispatch solutions.
    """
    cost_objective = "cost_with_tracking" if temperature_mode == "tracking" else "cost"
    cost_optimum = solve_model(data, temperature_mode, objective=cost_objective)
    emissions_optimum = solve_model(data, temperature_mode, objective="emissions")

    if cost_optimum["status"] != "Optimal" or emissions_optimum["status"] != "Optimal":
        raise RuntimeError("A feasible scenario is required for Pareto analysis.")

    include_tracking = temperature_mode == "tracking"
    anchor_cost = (cost_optimum["economic_cost"]
                   + (cost_optimum["tracking_cost"] if include_tracking else 0.0))
    scales = {
        "cost": max(anchor_cost, 1.0),
        "emissions": max(emissions_optimum["emissions_kg"], 1.0),
    }

    points = []
    for alpha in np.linspace(0, 1, n_points):
        r = solve_model(data, temperature_mode, objective="pareto_cost_emissions",
                        alpha=float(alpha), scales=scales)
        if r["status"] != "Optimal":
            raise RuntimeError(f"Pareto point alpha={alpha:.2f} did not solve to optimality.")
        # Store comprehensive cost for plotting
        r["cost_for_pareto"] = (r["economic_cost"]
                                + (r["tracking_cost"] if include_tracking else 0.0))
        points.append(r)

    return points


def sensitivity_analysis(data, kind, alpha_values=SENSITIVITY_ALPHAS):
    """Solve parametric coefficient sensitivity analyses using the requested alphas.

    Modes:
      â€¢ 'tracking'  : Solves Cost_economic + alpha_T * tracking_error under soft tracking mode.
      â€¢ 'emissions' : Solves Cost_economic + alpha_E * Emissions under hard temperature bounds.

    Args:
        data (dict): System parameters.
        kind (str): 'tracking' or 'emissions'.
        alpha_values (tuple[float]): Parameter values to evaluate.

    Returns:
        list[dict]: List of sensitivity dispatch results.
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
    """Print one Pareto curve as a compact console table."""
    print(f"\n--- {title} ---")
    print(" alpha   cost [EUR]   emissions [kg CO2]   tracking [EUR]   status")
    for p in points:
        print(f"{p['alpha']:6.2f}   {p['cost_for_pareto']:10.2f}   {p['emissions_kg']:18.1f}"
              f"   {p['tracking_cost']:14.2f}   {p['status']}")


def print_tracking_sensitivity(points):
    """Print Sensitivity 1: Total cost vs. temperature-tracking error table."""
    print("\n--- SENSITIVITY 1: total cost vs temperature-tracking error ---")
    print(" alpha_T   economic cost [EUR]   tracking error [degC h]   status")
    for p in points:
        print(f"{p['sensitivity_alpha']:7.2f}   {p['economic_cost']:19.2f}"
              f"   {p['tracking_error_degC_h']:23.3f}   {p['status']}")


def print_emissions_sensitivity(points):
    """Print Sensitivity 2: Total cost vs. physical CO2 emissions table."""
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
# PART 6 - POWER-BALANCE VISUALISATION & AUDIT EXPORT
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
    """Save an hourly stacked power-balance chart for the selected microgrid in PuLP."""
    if result["status"] != "Optimal":
        raise ValueError("Power balances can only be plotted for an optimal dispatch.")

    hours = np.arange(1, data["SYS"]["T"] + 1)
    f12, f23 = np.asarray(result["f12"]), np.asarray(result["f23"])

    if microgrid == "MG1":
        positive = [
            ("fR used", np.asarray(data["MG1"]["fR"]) - result["fR1_curt"], "#5B9A68"),
            ("fG import", np.maximum(result["fG1"], 0), "#F4A261"),
            ("fNR", np.asarray(result["fNR1"]), "#B8744F"),
            ("fS discharge", np.maximum(result["fS1"], 0), "#7CB342"),
            ("MG2 to MG1", np.maximum(-f12, 0), "#455A64"),
        ]
        negative = [
            ("fL", data["MG1"]["fL"], "#4F81A2"),
            ("fG export", np.maximum(-np.asarray(result["fG1"]), 0), "#56B4E9"),
            ("fS charge", np.maximum(-np.asarray(result["fS1"]), 0), "#B65BAA"),
            ("MG1 to MG2", np.maximum(f12, 0), "#3D6B43"),
        ]
        title = "POWER BALANCE OF MICROGRID 1"
    elif microgrid == "MG2":
        positive = [
            ("fR used", np.asarray(data["MG2"]["fR"]) - result["fR2_curt"], "#5B9A68"),
            ("EV discharge", np.maximum(result["fEV"], 0), "#7CB342"),
            ("MG1 to MG2", np.maximum(f12, 0), "#455A64"),
            ("MG3 to MG2", np.maximum(-f23, 0), "#546E7A"),
        ]
        negative = [
            ("fL", data["MG2"]["fL"], "#4F81A2"),
            ("EV charge", np.maximum(-np.asarray(result["fEV"]), 0), "#B65BAA"),
            ("heat pump", np.asarray(result["fHP"]), "#F4A261"),
            ("MG2 to MG1", np.maximum(-f12, 0), "#3D6B43"),
            ("MG2 to MG3", np.maximum(f23, 0), "#8064A2"),
        ]
        title = "POWER BALANCE OF MICROGRID 2"
    elif microgrid == "MG3":
        positive = [
            ("fR used", np.asarray(data["MG3"]["fR"]) - result["fR3_curt"], "#5B9A68"),
            ("fG import", np.maximum(result["fG3"], 0), "#F4A261"),
            ("fS discharge", np.maximum(result["fS3"], 0), "#7CB342"),
            ("MG2 to MG3", np.maximum(f23, 0), "#455A64"),
        ]
        negative = [
            ("fL", data["MG3"]["fL"], "#4F81A2"),
            ("fG export", np.maximum(-np.asarray(result["fG3"]), 0), "#56B4E9"),
            ("fS charge", np.maximum(-np.asarray(result["fS3"]), 0), "#B65BAA"),
            ("MG3 to MG2", np.maximum(-f23, 0), "#3D6B43"),
        ]
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
    """Print one valid dispatch table in the project notation."""
    print(f"\n--- {title} ---\nStatus: {result['status']}")
    if result["status"] != "Optimal":
        print(result["message"])
        return
    print(f"Economic cost: EUR {result['economic_cost']:.2f} | CO2: {result['emissions_kg']:.1f} kg | ENS: {result['ENS_kWh']:.2f} kWh")
    print(" h  fNR1    fG1    fS1    x1    fEV   xEV   fHP    Tb    fG3    fS3    x3    f12    f23    ENS")
    for t in range(24):
        print(f"{t + 1:2d} {result['fNR1'][t]:6.1f} {result['fG1'][t]:6.1f} {result['fS1'][t]:6.1f} {result['x1'][t + 1]:5.2f} "
              f"{result['fEV'][t]:6.1f} {result['xEV'][t + 1]:5.2f} {result['fHP'][t]:5.1f} {result['Tb'][t + 1]:5.2f} "
              f"{result['fG3'][t]:6.1f} {result['fS3'][t]:6.1f} {result['x3'][t + 1]:5.2f} {result['f12'][t]:6.1f} {result['f23'][t]:6.1f} {result['ENS'][t]:6.1f}")


def validate(result, data, tol=1e-4):
    """Audit the feasible planning dispatch against core physical requirements."""
    if result["status"] != "Optimal":
        print(f"Validation skipped: {result['status']}")
        return

    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    residual = 0.0
    for t in range(sys["T"]):
        residual = max(
            residual,
            abs(result["fG1"][t] + result["fNR1"][t] + mg1["fR"][t] - result["fR1_curt"][t] + result["fS1"][t] - mg1["fL"][t] - result["f12"][t]),
            abs(mg2["fR"][t] - result["fR2_curt"][t] + result["fEV"][t] + result["f12"][t] - mg2["fL"][t] - result["fHP"][t] - result["f23"][t]),
            abs(result["fG3"][t] + mg3["fR"][t] - result["fR3_curt"][t] + result["fS3"][t] + result["f23"][t] - mg3["fL"][t]),
        )

    checks = {
        "Hourly power balances": residual <= tol,
        "Zero ENS load shedding": max(result["ENS"]) <= tol,
        "EV departure SOC >= 0.80": result["xEV"][18] >= mg2["xEVdeadline"] - tol,
        "Terminal stationary BESS SOC restored": result["x1"][-1] >= mg1["xin"] - tol and result["x3"][-1] >= mg3["xin"] - tol,
        "SOC within bounds [xmin, xmax]": min(result["x1"]) >= mg1["xmin"] - tol and max(result["x1"]) <= mg1["xmax"] + tol and min(result["x3"]) >= mg3["xmin"] - tol and max(result["x3"]) <= mg3["xmax"] + tol,
        "Building temperature bounds [19, 23] C": min(result["Tb"][1:]) >= mg2["Tmin"] - tol and max(result["Tb"][1:]) <= mg2["Tmax"] + tol,
    }

    print("\n--- PULP PHYSICAL VALIDATION AUDIT ---")
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    print(f"  Maximum power-balance residual: {residual:.1e} kW")


def export_results(planning, pareto_tracking, pareto_bounds, tracking_sensitivity, emissions_sensitivity,
                   path=RESULTS_DIR / "results_pulp.xlsx"):
    """Export dispatch plus both Pareto curves and both sensitivity analyses to Excel."""
    RESULTS_DIR.mkdir(exist_ok=True)
    dispatch = pd.DataFrame({
        "hour": range(1, 25),
        "fNR1_kW": planning["fNR1"],
        "fG1_kW": planning["fG1"],
        "fS1_kW": planning["fS1"],
        "x1_end": planning["x1"][1:],
        "fEV_kW": planning["fEV"],
        "xEV_end": planning["xEV"][1:],
        "fHP_kW": planning["fHP"],
        "Tb_end_C": planning["Tb"][1:],
        "fG3_kW": planning["fG3"],
        "fS3_kW": planning["fS3"],
        "x3_end": planning["x3"][1:],
        "fR1_curt_kW": planning["fR1_curt"],
        "fR2_curt_kW": planning["fR2_curt"],
        "fR3_curt_kW": planning["fR3_curt"],
        "f12_kW": planning["f12"],
        "f23_kW": planning["f23"],
        "ENS_kW": planning["ENS"],
    }).round(3)

    def table(points, alpha_key):
        return pd.DataFrame([{
            alpha_key: p.get(alpha_key, p["alpha"]),
            "economic_cost_EUR": p["economic_cost"],
            "pareto_cost_EUR": p.get("cost_for_pareto", p["economic_cost"]),
            "CO2_kg": p["emissions_kg"],
            "tracking_error_degC_h": p["tracking_error_degC_h"],
        } for p in points]).round(3)

    def write(target):
        with pd.ExcelWriter(target) as writer:
            dispatch.to_excel(writer, sheet_name="planning_dispatch", index=False)
            table(pareto_tracking, "pareto_alpha").to_excel(writer, sheet_name="pareto_1_tracking", index=False)
            table(pareto_bounds, "pareto_alpha").to_excel(writer, sheet_name="pareto_2_bounds", index=False)
            table(tracking_sensitivity, "alpha_T").to_excel(writer, sheet_name="tracking_sensitivity", index=False)
            table(emissions_sensitivity, "alpha_E").to_excel(writer, sheet_name="emissions_sensitivity", index=False)

    try:
        write(path)
    except PermissionError:
        path = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        write(path)

    print(f"Results workbook saved to {path}")


# ============================================================================
# MAIN EXECUTION ENTRYPOINT
# ============================================================================
if __name__ == "__main__":
    print("Input file:", DATA_FILE)
    print("Notation: fL load, fR renewables, fG grid, fNR fossil, fS storage, x SOC.")

    # 1. Run Diagnostic Bottleneck Evaluation
    diagnostic_result = solve_model(load_data(scenario="diagnostic"), objective="cost")
    print_results(diagnostic_result, "Diagnostic: original 200-kW grid and line limits")

    # 2. Run Feasible Planning Upgrade Scenario
    planning_data = load_data(scenario="capacity_upgrade")
    planning_result = solve_model(planning_data, objective="cost")
    print_results(planning_result, "Feasible planning scenario: 300-kW grids and 500-kW lines")

    # 3. Perform Physical Audit Validation
    validate(planning_result, planning_data)

    # 4. Generate & Save Power-Balance Stacked Bar Charts
    print("\n--- POWER-BALANCE FIGURES ---")
    plot_power_balance(planning_result, planning_data, "MG1", "power_balance_MG1.png")
    plot_power_balance(planning_result, planning_data, "MG2", "power_balance_MG2.png")
    plot_power_balance(planning_result, planning_data, "MG3", "power_balance_MG3.png")

    # 5. Execute Multi-Objective Pareto Frontier Analyses
    pareto_tracking = pareto_cost_emissions(planning_data, temperature_mode="tracking")
    pareto_bounds = pareto_cost_emissions(planning_data, temperature_mode="bounds")

    # 6. Execute Parametric Sensitivity Sweeps
    tracking_sensitivity = sensitivity_analysis(planning_data, "tracking")
    emissions_sensitivity = sensitivity_analysis(planning_data, "emissions")

    # 7. Print Summary Tables to Console
    print_pareto(pareto_tracking, "PARETO 1: costs (energy + carbon + tracking) vs emissions")
    print_pareto(pareto_bounds, "PARETO 2: costs (energy + carbon) vs emissions; 19-23 C bounds")
    print_tracking_sensitivity(tracking_sensitivity)
    print_emissions_sensitivity(emissions_sensitivity)

    # 8. Render & Save Pareto and Sensitivity Figures
    print("\n--- PARETO AND SENSITIVITY FIGURES ---")
    plot_cost_emissions_pareto(
        pareto_tracking,
        "Cost (carbon + tracking) vs CO2 emissions",
        "pareto_1_initial_cost_vs_emissions.png"
    )
    plot_cost_emissions_pareto(
        pareto_bounds,
        "Cost vs CO2 emissions (hard temperature bounds)",
        "pareto_2_bounds_cost_vs_emissions.png"
    )
    plot_tracking_sensitivity(tracking_sensitivity)
    plot_emissions_sensitivity(emissions_sensitivity)

    # 9. Export All Numerical Datasets to Excel Workbook
    export_results(planning_result, pareto_tracking, pareto_bounds, tracking_sensitivity, emissions_sensitivity)
    print(f"\nAll PuLP figures and Excel results saved to {RESULTS_DIR}")
