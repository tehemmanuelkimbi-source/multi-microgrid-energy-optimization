"""Three Interconnected Microgrids: 24-Hour Convex MIQP Day-Ahead Scheduling.

Mathematical Formulation & Optimization Framework:
==================================================
This module implements a day-ahead operational scheduling model for three
interconnected multi-energy microgrids (MMGs) using Pyomo and Gurobi.

Optimization Class:
  Mixed-Integer Quadratic Program (MIQP) / Mixed-Integer Linear Program (MILP).
  - Exact quadratic comfort tracking: J_track = sum_t (Tb(t) - T_set)^2
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
  x, xEV    : State of Charge (SOC) fraction [0.0 to 1.0]
  f12, f23  : Inter-microgrid tie-line power flows [kW] (positive: forward flow)
  ENS       : Energy Not Served / load shedding slack [kW]
"""

from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition

# ============================================================================
# FILE SYSTEM & GLOBAL CONFIGURATION
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_FILE = PROJECT_ROOT / "data" / "data_m.xlsx"
RESULTS_DIR = PROJECT_ROOT / "outputs" / "pyomo_gurobi"

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
    # Load worksheet without header assumptions to robustly slice by numerical hour
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
    storage = {
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
            **storage,
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
            "people": np.array([0] * 7 + [25] * 5 + [0] * 1  + [25] * 2 + [0] * 9, dtype=float),
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
            **storage,
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
            "fG_SYSmax": 1000.0,   # Main grid total simultaneous substation import limit [kW]
            "C_CO2": 0.030,        # Base carbon tax / emission price [EUR / kg CO2]
            "eNR": 0.3706,         # Fossil generator emission intensity [kg CO2 / kWh]
            "eGrid": 0.1752,       # Main grid electricity emission factor [kg CO2 / kWh]
            "f12max": 200.0,       # Line capacity MG1 <-> MG2 [kW]
            "f23max": 200.0,       # Line capacity MG2 <-> MG3 [kW]
            "VoLL": 10.0,          # Value of Lost Load penalty for ENS [EUR / kWh]
            "thetaT": 100.0,        # Virtual comfort penalty weight [EUR / (degC^2 * h)]
            "allow_ENS": scenario == "diagnostic",  # Enable ENS slacks only in diagnostic mode
        },
    }

    # Apply upgraded transmission and substation capacities for the planning scenario
    if scenario == "capacity_upgrade":
        data["MG1"]["fGmax"] = data["MG3"]["fGmax"] = 300.0
        data["SYS"]["f12max"] = data["SYS"]["f23max"] = 500.0

    return data

# ============================================================================
# PART 2 - PYOMO CONCRETE MODEL & DECISION VARIABLES
# Goal: Define indexed Sets, Parameters, Continuous Power Vars, Binary Vars.
# ============================================================================
def create_model(data):
    """Instantiate the Pyomo ConcreteModel with all Sets, Params, and Vars.

    Decision Variables Created:
      â€¢ Grid:       fG1_in, fG1_out, uG1, fG3_in, fG3_out, uG3
      â€¢ Storage:    fS1_C, fS1_D, uS1, x1, fS3_C, fS3_D, uS3, x3
      â€¢ Generation: fNR1, fR1_C, fR2_C, fR3_C (curtailment)
      â€¢ Demand/HP:  fEV_C, fEV_D, uEV, xEV, fHP, Tb
      â€¢ Lines:      f12, f23 (signed inter-microgrid exchanges)
      â€¢ Slacks:     ENS1, ENS2, ENS3 (Energy Not Served)

    Args:
        data (dict): System parameters dictionary from load_data().

    Returns:
        pyo.ConcreteModel: Pyomo model initialized with variables.
    """
    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    m = pyo.ConcreteModel("Three_Microgrid_MIQP")

    # Time Index Sets:
    # H       : Dispatch time steps t = 0, ..., 23 (hourly intervals)
    # H_STATE : State variable indices t = 0, ..., 24 (includes t = 0 initial & t = 24 terminal)
    m.H = pyo.RangeSet(0, sys["T"] - 1)
    m.H_STATE = pyo.RangeSet(0, sys["T"])

    # Load exogenous time-series as Pyomo Params
    init_param = lambda values: {t: float(values[t]) for t in range(sys["T"])}
    for name, values in {
        "fL1": mg1["fL"], "fR1": mg1["fR"], "Bp1": mg1["Bp"], "Sp1": mg1["Sp"],
        "fL2": mg2["fL"], "fR2": mg2["fR"], "Text": mg2["Text"], "people": mg2["people"],
        "fL3": mg3["fL"], "fR3": mg3["fR"], "Bp3": mg3["Bp"], "Sp3": mg3["Sp"],
    }.items():
        setattr(m, name, pyo.Param(m.H, initialize=init_param(values), within=pyo.Reals))

    # ------------------------------------------------------------------------
    # MICROGRID 1 VARIABLES
    # ------------------------------------------------------------------------
    # Grid import/export power and binary direction flag (uG1=1: import, uG1=0: export)
    m.fG1_in = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg1["fGmax"]))
    m.fG1_out = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg1["fGmax"]))
    m.uG1 = pyo.Var(m.H, domain=pyo.Binary)

    # Fossil generator power output [kW]
    m.fNR1 = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg1["fNRmax"]))

    # Stationary battery charging/discharging [kW], binary mode (uS1=1: discharge), and SOC
    m.fS1_C = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg1["fSmax"]))
    m.fS1_D = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg1["fSmax"]))
    m.uS1 = pyo.Var(m.H, domain=pyo.Binary)
    m.x1 = pyo.Var(m.H_STATE, bounds=(mg1["xmin"], mg1["xmax"]))

    # ------------------------------------------------------------------------
    # MICROGRID 3 VARIABLES
    # ------------------------------------------------------------------------
    m.fG3_in = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg3["fGmax"]))
    m.fG3_out = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg3["fGmax"]))
    m.uG3 = pyo.Var(m.H, domain=pyo.Binary)

    m.fS3_C = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg3["fSmax"]))
    m.fS3_D = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg3["fSmax"]))
    m.uS3 = pyo.Var(m.H, domain=pyo.Binary)
    m.x3 = pyo.Var(m.H_STATE, bounds=(mg3["xmin"], mg3["xmax"]))

    # ------------------------------------------------------------------------
    # MICROGRID 2 VARIABLES (EV, Heat Pump, Thermal State)
    # ------------------------------------------------------------------------
    m.fEV_C = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg2["fEVmax"]))
    m.fEV_D = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg2["fEVmax"]))
    m.uEV = pyo.Var(m.H, domain=pyo.Binary)
    m.xEV = pyo.Var(m.H_STATE, bounds=(mg2["xEVmin"], mg2["xEVmax"]))

    # Heat pump electrical consumption [kW] and indoor temperature state [degC]
    m.fHP = pyo.Var(m.H, domain=pyo.NonNegativeReals, bounds=(0, mg2["fHPmax"]))
    m.Tb = pyo.Var(m.H_STATE, bounds=(5.0, 35.0))

    # Renewable curtailment variables [kW] (prevents artificial infeasibility when RES > load+storage)
    m.fR1_C = pyo.Var(m.H, domain=pyo.NonNegativeReals)
    m.fR2_C = pyo.Var(m.H, domain=pyo.NonNegativeReals)
    m.fR3_C = pyo.Var(m.H, domain=pyo.NonNegativeReals)

    # ------------------------------------------------------------------------
    # INTERCONNECTION LINES & DIAGNOSTIC ENERGY-NOT-SERVED (ENS)
    # ------------------------------------------------------------------------
    # Signed line flows: f12 > 0 is MG1 -> MG2; f23 > 0 is MG2 -> MG3
    m.f12 = pyo.Var(m.H, domain=pyo.Reals, bounds=(-sys["f12max"], sys["f12max"]))
    m.f23 = pyo.Var(m.H, domain=pyo.Reals, bounds=(-sys["f23max"], sys["f23max"]))

    # ENS slack variables [kW] (active only in diagnostic mode)
    m.ENS1 = pyo.Var(m.H, domain=pyo.NonNegativeReals)
    m.ENS2 = pyo.Var(m.H, domain=pyo.NonNegativeReals)
    m.ENS3 = pyo.Var(m.H, domain=pyo.NonNegativeReals)

    return m


# ============================================================================
# PART 3 - PHYSICAL AND OPERATIONAL CONSTRAINTS
# Goal: Enforce nodal power balance, SOC dynamics, Big-M exclusions, thermal RC.
# ============================================================================
def add_constraints(m, data, temperature_mode="bounds"):
    """Attach all physical and operational constraints to the Pyomo model.

    Constraints Formulated:
      1. Nodal Power Balances (Kirchhoff's Current Law at each MG bus)
      2. Renewable Curtailment Upper Bounds (fR_C <= fR)
      3. Big-M Complementarity Mutual Exclusions (Prevent simultaneous charge/discharge & import/export)
      4. Macrogrid Substation Total Import Capacity
      5. Battery & EV State-of-Charge Linear Difference Equations & Terminal Bounds
      6. EV 18:00 Departure SOC Requirement & Post-Departure Disconnection
      7. 1st-Order Building Thermal Mass Difference Equations (RC lumped dynamics)
      8. Thermal Comfort Handling (Soft Tracking Penalty vs Hard Comfort Box [19, 23] degC)

    Args:
        m (pyo.ConcreteModel): Pyomo model.
        data (dict): System parameters.
        temperature_mode (str): 'bounds' (hard 19-23 C) or 'tracking' (penalized J_track).
    """
    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    dt = sys["dt"]

    # ------------------------------------------------------------------------
    # 1. NODAL ELECTRICAL POWER BALANCES [kW]
    # Supply + Inflows = Demand + Storage Charging + Outflows
    # ------------------------------------------------------------------------
    # MG1: Grid_in - Grid_out + Fossil + (PV - Curt) + Batt_disch - Batt_ch + ENS1 == Load + Line_12
    m.MG1_balance = pyo.Constraint(
        m.H,
        rule=lambda m, t: (
            m.fG1_in[t] - m.fG1_out[t] + m.fNR1[t] + m.fR1[t] - m.fR1_C[t]
            + m.fS1_D[t] - m.fS1_C[t] + m.ENS1[t]
            == m.fL1[t] + m.f12[t]
        )
    )

    # MG2: (Wind - Curt) + EV_disch - EV_ch + Line_12 + ENS2 == Load + HeatPump + Line_23
    m.MG2_balance = pyo.Constraint(
        m.H,
        rule=lambda m, t: (
            m.fR2[t] - m.fR2_C[t] + m.fEV_D[t] - m.fEV_C[t] + m.f12[t] + m.ENS2[t]
            == m.fL2[t] + m.fHP[t] + m.f23[t]
        )
    )

    # MG3: Grid_in - Grid_out + (RES - Curt) + Batt_disch - Batt_ch + Line_23 + ENS3 == Load
    m.MG3_balance = pyo.Constraint(
        m.H,
        rule=lambda m, t: (
            m.fG3_in[t] - m.fG3_out[t] + m.fR3[t] - m.fR3_C[t]
            + m.fS3_D[t] - m.fS3_C[t] + m.f23[t] + m.ENS3[t]
            == m.fL3[t]
        )
    )

    # ------------------------------------------------------------------------
    # 2. RENEWABLE CURTAILMENT LIMITS [kW]
    # ------------------------------------------------------------------------
    m.RES1_curtailment = pyo.Constraint(m.H, rule=lambda m, t: m.fR1_C[t] <= m.fR1[t])
    m.RES2_curtailment = pyo.Constraint(m.H, rule=lambda m, t: m.fR2_C[t] <= m.fR2[t])
    m.RES3_curtailment = pyo.Constraint(m.H, rule=lambda m, t: m.fR3_C[t] <= m.fR3[t])

    # In planning scenarios, force Energy Not Served slacks to zero
    if not sys["allow_ENS"]:
        m.ENS_not_allowed = pyo.Constraint(
            m.H, rule=lambda m, t: m.ENS1[t] + m.ENS2[t] + m.ENS3[t] == 0
        )

    # ------------------------------------------------------------------------
    # 3. BIG-M MUTUAL-EXCLUSION CONSTRAINTS
    # Ensures unidirectional power flow at any single hour t
    # ------------------------------------------------------------------------
    # MG1 Grid Import / Export exclusion
    m.ME_G1_in = pyo.Constraint(m.H, rule=lambda m, t: m.fG1_in[t] <= mg1["fGmax"] * m.uG1[t])
    m.ME_G1_out = pyo.Constraint(m.H, rule=lambda m, t: m.fG1_out[t] <= mg1["fGmax"] * (1 - m.uG1[t]))

    # MG3 Grid Import / Export exclusion
    m.ME_G3_in = pyo.Constraint(m.H, rule=lambda m, t: m.fG3_in[t] <= mg3["fGmax"] * m.uG3[t])
    m.ME_G3_out = pyo.Constraint(m.H, rule=lambda m, t: m.fG3_out[t] <= mg3["fGmax"] * (1 - m.uG3[t]))

    # MG1 Battery Charge / Discharge exclusion
    m.ME_S1_D = pyo.Constraint(m.H, rule=lambda m, t: m.fS1_D[t] <= mg1["fSmax"] * m.uS1[t])
    m.ME_S1_C = pyo.Constraint(m.H, rule=lambda m, t: m.fS1_C[t] <= mg1["fSmax"] * (1 - m.uS1[t]))

    # MG3 Battery Charge / Discharge exclusion
    m.ME_S3_D = pyo.Constraint(m.H, rule=lambda m, t: m.fS3_D[t] <= mg3["fSmax"] * m.uS3[t])
    m.ME_S3_C = pyo.Constraint(m.H, rule=lambda m, t: m.fS3_C[t] <= mg3["fSmax"] * (1 - m.uS3[t]))

    # MG2 Electric Vehicle Charge / Discharge exclusion
    m.ME_EV_D = pyo.Constraint(m.H, rule=lambda m, t: m.fEV_D[t] <= mg2["fEVmax"] * m.uEV[t])
    m.ME_EV_C = pyo.Constraint(m.H, rule=lambda m, t: m.fEV_C[t] <= mg2["fEVmax"] * (1 - m.uEV[t]))

    # 4. Total Main grid Substation Import Limit
    m.system_grid = pyo.Constraint(
        m.H, rule=lambda m, t: m.fG1_in[t] + m.fG3_in[t] <= sys["fG_SYSmax"]
    )

    # ------------------------------------------------------------------------
    # 5. STATIONARY BATTERY STORAGE STATE-OF-CHARGE (SOC) DYNAMICS
    # x(t+1) = beta * x(t) + (dt / CAPsto) * (eta_ch * fS_C(t) - eta_disch_factor * fS_D(t))
    # ------------------------------------------------------------------------
    m.x1_initial = pyo.Constraint(expr=m.x1[0] == mg1["xin"])
    m.x3_initial = pyo.Constraint(expr=m.x3[0] == mg3["xin"])

    m.x1_state = pyo.Constraint(
        m.H,
        rule=lambda m, t: m.x1[t + 1] == mg1["beta"] * m.x1[t] + dt * (
            mg1["eta_ch"] * m.fS1_C[t] - mg1["eta_disch_factor"] * m.fS1_D[t]
        ) / mg1["CAPsto"]
    )
    m.x3_state = pyo.Constraint(
        m.H,
        rule=lambda m, t: m.x3[t + 1] == mg3["beta"] * m.x3[t] + dt * (
            mg3["eta_ch"] * m.fS3_C[t] - mg3["eta_disch_factor"] * m.fS3_D[t]
        ) / mg3["CAPsto"]
    )

    # Terminal SOC no lower than the initial SOC.
    m.x1_terminal = pyo.Constraint(expr=m.x1[sys["T"]] >= mg1["xin"])
    m.x3_terminal = pyo.Constraint(expr=m.x3[sys["T"]] >= mg3["xin"])

    # ------------------------------------------------------------------------
    # 6. ELECTRIC VEHICLE (EV) DYNAMICS & MOBILITY CONSTRAINTS
    # ------------------------------------------------------------------------
    m.xEV_initial = pyo.Constraint(expr=m.xEV[0] == mg2["xEVin"])
    m.xEV_state = pyo.Constraint(
        m.H,
        rule=lambda m, t: m.xEV[t + 1] == mg2["beta_EV"] * m.xEV[t] + dt * (
            mg2["eta_ch_EV"] * m.fEV_C[t] - mg2["eta_disch_EV_factor"] * m.fEV_D[t]
        ) / mg2["CAP_EV"]
    )

    # Minimum SOC guarantee before departure at 18:00 (xEV >= 80%)
    m.EV_deadline = pyo.Constraint(expr=m.xEV[mg2["EV_departure"]] >= mg2["xEVdeadline"])

    # Enforce zero charging / discharging when EV is away from the microgrid (t >= 18)
    departed = range(mg2["EV_departure"], sys["T"])
    m.EV_absent_C = pyo.Constraint(departed, rule=lambda m, t: m.fEV_C[t] == 0)
    m.EV_absent_D = pyo.Constraint(departed, rule=lambda m, t: m.fEV_D[t] == 0)

    # ------------------------------------------------------------------------
    # 7. BUILDING THERMAL DYNAMICS (1st-Order Lumped RC Equivalent Model)
    # Tb(t+1) = Tb(t) + (dt / CB) * [ Q_HP(t) + Q_envelope(t) + Q_internal(t) ]
    # where: Q_HP = EER_HP * fHP, Q_envelope = (Text - Tb) / Rext, Q_int = Q_person * people
    # ------------------------------------------------------------------------
    m.Tb_initial = pyo.Constraint(expr=m.Tb[0] == mg2["T0"])
    m.Tb_state = pyo.Constraint(
        m.H,
        rule=lambda m, t: m.Tb[t + 1] == m.Tb[t] + dt / mg2["CB"] * (
            mg2["EER_HP"] * m.fHP[t]
            + (m.Text[t] - m.Tb[t]) / mg2["Rext"]
            + mg2["Qint_person"] * m.people[t]
        )
    )

    # ------------------------------------------------------------------------
    # 8. THERMAL COMFORT TREATMENT
    # 'bounds'   : Strict physical hard bounds [19 degC, 23 degC]
    # 'tracking' : Soft tracking error (Tb - Tset)^2 penalized in the objective
    # ------------------------------------------------------------------------
    if temperature_mode == "bounds":
        m.T_min = pyo.Constraint(m.H, rule=lambda m, t: m.Tb[t + 1] >= mg2["Tmin"])
        m.T_max = pyo.Constraint(m.H, rule=lambda m, t: m.Tb[t + 1] <= mg2["Tmax"])
    elif temperature_mode != "tracking":
        raise ValueError("temperature_mode must be 'bounds' or 'tracking'")


# ============================================================================
# PART 4 - OBJECTIVE FUNCTIONS, SOLUTION PIPELINE, RESULT EXTRACTION
# ============================================================================
def add_expressions(m, data):
    """Define named Pyomo Expressions for costs, emissions, tracking, and ENS.

    Expressions:
      â€¢ energy_cost   : Net grid import cost minus export revenue + fossil fuel costs [EUR]
      â€¢ emissions_kg  : Physical greenhouse gas emissions from fossil gen & grid imports [kg CO2]
      â€¢ carbon_cost   : Baseline regulatory carbon cost (C_CO2 * emissions_kg) [EUR]
      â€¢ economic_cost : Total financial expenditure (energy_cost + carbon_cost) [EUR]
      â€¢ J_track       : Cumulative quadratic temperature-tracking error [degC^2 * h]
      â€¢ tracking_cost : Virtual monetary discomfort penalty (thetaT * J_track) [EUR]
      â€¢ ENS_kWh       : Total unserved energy volume [kWh]

    Args:
        m (pyo.ConcreteModel): Pyomo model.
        data (dict): System parameters.
    """
    mg1, sys = data["MG1"], data["SYS"]

    # 1. Total Daily Energy Operating Cost [EUR/day]
    m.energy_cost = pyo.Expression(
        expr=sum(
            (
                m.Bp1[t] * m.fG1_in[t] - m.Sp1[t] * m.fG1_out[t]
                + m.Bp3[t] * m.fG3_in[t] - m.Sp3[t] * m.fG3_out[t]
                + mg1["CNR"] * m.fNR1[t]
            ) * sys["dt"]
            for t in m.H
        )
    )

    # 2. Total Daily Physical CO2 Emissions [kg CO2/day]
    m.emissions_kg = pyo.Expression(
        expr=sum(
            (
                sys["eNR"] * m.fNR1[t]
                + sys["eGrid"] * (m.fG1_in[t] + m.fG3_in[t])
            ) * sys["dt"]
            for t in m.H
        )
    )

    # 3. Base Carbon Tax Cost [EUR/day] (C_CO2 = 0.030 EUR/kg)
    m.carbon_cost = pyo.Expression(expr=sys["C_CO2"] * m.emissions_kg)

    # 4. Total Pure Economic Expenditure [EUR/day]
    m.economic_cost = pyo.Expression(expr=m.energy_cost + m.carbon_cost)

    # 5. Exact Quadratic Temperature Tracking Error, with dt=1h, the discrete sum is reported in [degC^2 * h]
    # Sums deviation of beginning-of-hour temperature from T_set = 21 degC
    m.J_track = pyo.Expression(expr=sum((m.Tb[t] - data["MG2"]["Tset"]) ** 2 for t in m.H))

    # 6. Virtual Thermal Discomfort Cost [EUR/day]
    m.tracking_cost = pyo.Expression(expr=sys["thetaT"] * m.J_track)

    # 7. Unserved Energy Slack Volume [kWh/day]
    m.ENS_kWh = pyo.Expression(expr=sys["dt"] * sum(m.ENS1[t] + m.ENS2[t] + m.ENS3[t] for t in m.H))


def solve_model(data, temperature_mode="bounds", objective="cost", alpha=1.0, scales=None):
    """Assemble, solve, and extract numerical results from the Pyomo MIQP model.

    Objectives Supported:
      â€¢ 'cost'                  : Minimize pure economic cost (energy + base carbon)
      â€¢ 'cost_with_tracking'    : Minimize economic cost + comfort tracking penalty
      â€¢ 'emissions'             : Minimize total physical CO2 emissions [kg]
      â€¢ 'tracking'              : Minimize quadratic thermal tracking error J_track
      â€¢ 'pareto_cost_emissions' : Normalized weighted-sum of cost and emissions:
                                  min [ alpha * (Cost/Scost) + (1-alpha) * (Emissions/Semiss) ]
      â€¢ 'sensitivity_tracking'  : Parametric sweep of comfort penalty alpha_T:
                                  min [ Cost_economic + alpha_T * J_track ]
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
    m = create_model(data)
    add_constraints(m, data, temperature_mode)
    add_expressions(m, data)

    # Objective Function Selection
    if objective == "cost":
        target = m.economic_cost
    elif objective == "cost_with_tracking":
        target = m.economic_cost + m.tracking_cost
    elif objective == "emissions":
        target = m.emissions_kg
    elif objective == "tracking":
        target = m.J_track
    elif objective == "pareto_cost_emissions":
        if scales is None:
            raise ValueError("Pareto objective requires normalisation scales")
        cost_part = m.economic_cost + (m.tracking_cost if temperature_mode == "tracking" else 0)
        target = alpha * cost_part / scales["cost"] + (1 - alpha) * m.emissions_kg / scales["emissions"]
    elif objective == "sensitivity_tracking":
        # alpha_T has physical units EUR / (degC^2 * h)
        target = m.economic_cost + alpha * m.J_track
    elif objective == "sensitivity_emissions":
        # alpha_E has physical units EUR / kg CO2 and acts as an incremental carbon tax
        target = m.economic_cost + alpha * m.emissions_kg
    else:
        raise ValueError(f"Unknown objective: {objective}")

    # Attach objective with high VoLL penalty on Energy Not Served
    m.objective = pyo.Objective(expr=target + data["SYS"]["VoLL"] * m.ENS_kWh, sense=pyo.minimize)

    # Solve using Gurobi MIQP Solver
    solver = pyo.SolverFactory("gurobi")
    if not solver.available(exception_flag=False):
        raise RuntimeError("Gurobi is unavailable. Check installed license in this environment.")
    solver.options["MIPGap"] = 1e-6
    solver.options["TimeLimit"] = 120
    solution = solver.solve(m, tee=False)

    optimal = (
        solution.solver.status == SolverStatus.ok
        and solution.solver.termination_condition == TerminationCondition.optimal
    )
    if not optimal:
        return {
            "status": str(solution.solver.termination_condition),
            "message": "No dispatch is shown because the MIQP was not solved to optimality.",
        }

    # Extract numerical values from solver results
    value = pyo.value
    h = list(m.H)
    result = {
        "status": "Optimal",
        "objective": objective,
        "alpha": alpha,
        "energy_cost": value(m.energy_cost),
        "carbon_cost": value(m.carbon_cost),
        "economic_cost": value(m.economic_cost),
        "emissions_kg": value(m.emissions_kg),
        "tracking_error_degC2_h": value(m.J_track),
        "tracking_cost": value(m.tracking_cost),
        "ENS_kWh": value(m.ENS_kWh),
        # Net grid power: positive = import, negative = export
        "fG1": [value(m.fG1_in[t] - m.fG1_out[t]) for t in h],
        "fNR1": [value(m.fNR1[t]) for t in h],
        # Net stationary battery power: positive = discharge, negative = charge
        "fS1": [value(m.fS1_D[t] - m.fS1_C[t]) for t in h],
        "x1": [value(m.x1[t]) for t in m.H_STATE],
        # Net EV power: positive = discharge (V2G), negative = charge (G2V)
        "fEV": [value(m.fEV_D[t] - m.fEV_C[t]) for t in h],
        "xEV": [value(m.xEV[t]) for t in m.H_STATE],
        "fHP": [value(m.fHP[t]) for t in h],
        "Tb": [value(m.Tb[t]) for t in m.H_STATE],
        "fG3": [value(m.fG3_in[t] - m.fG3_out[t]) for t in h],
        "fS3": [value(m.fS3_D[t] - m.fS3_C[t]) for t in h],
        "x3": [value(m.x3[t]) for t in m.H_STATE],
        "fR1_curt": [value(m.fR1_C[t]) for t in h],
        "fR2_curt": [value(m.fR2_C[t]) for t in h],
        "fR3_curt": [value(m.fR3_C[t]) for t in h],
        "f12": [value(m.f12[t]) for t in h],
        "f23": [value(m.f23[t]) for t in h],
        "ENS": [value(m.ENS1[t] + m.ENS2[t] + m.ENS3[t]) for t in h],
    }
    return result


# ============================================================================
# PART 5 - PARETO FRONTIER & SENSITIVITY ENGINES
# ============================================================================
def pareto_cost_emissions(data, temperature_mode, n_points=11):
    """Generate the normalized weighted-sum Pareto frontier between Cost and CO2.

    Algorithm:
      1. Solve anchor point alpha = 1.0 (pure cost minimization) -> S_cost
      2. Solve anchor point alpha = 0.0 (pure emissions minimization) -> S_emissions
      3. For alpha in linspace(0, 1, n_points), solve normalized composite objective.

    Args:
        data (dict): System parameters.
        temperature_mode (str): 'tracking' (Pareto 1, soft) or 'bounds' (Pareto 2, hard).
        n_points (int): Number of trade-off evaluation points (default: 11).

    Returns:
        list[dict]: List of solved Pareto dispatch solution dictionaries.
    """
    if temperature_mode not in {"tracking", "bounds"}:
        raise ValueError("temperature_mode must be 'tracking' or 'bounds'")

    cost_objective = "cost_with_tracking" if temperature_mode == "tracking" else "cost"
    cost_optimum = solve_model(data, temperature_mode, cost_objective)
    emissions_optimum = solve_model(data, temperature_mode, "emissions")

    if cost_optimum["status"] != "Optimal" or emissions_optimum["status"] != "Optimal":
        raise RuntimeError("A feasible planning scenario is required for Pareto analysis.")

    include_tracking = temperature_mode == "tracking"
    scales = {
        "cost": max(cost_optimum["economic_cost"] + (cost_optimum["tracking_cost"] if include_tracking else 0.0), 1.0),
        "emissions": max(emissions_optimum["emissions_kg"], 1.0),
    }

    points = []
    for alpha in np.linspace(0, 1, n_points):
        r = solve_model(data, temperature_mode, "pareto_cost_emissions", float(alpha), scales)
        if r["status"] != "Optimal":
            raise RuntimeError(f"Pareto point alpha={alpha:.2f} did not solve to optimality.")
        # Store comprehensive cost for plotting
        r["cost_for_pareto"] = r["economic_cost"] + (r["tracking_cost"] if include_tracking else 0.0)
        points.append(r)

    return points


def sensitivity_analysis(data, kind, alpha_values=SENSITIVITY_ALPHAS):
    """Execute parametric sensitivity sweeps over physical/policy coefficients.

    Modes:
      â€¢ 'tracking'  : Sweeps alpha_T (comfort penalty) under soft tracking mode.
      â€¢ 'emissions' : Sweeps alpha_E (carbon tax) under hard temperature bounds [19, 23] C.

    Args:
        data (dict): System parameters.
        kind (str): 'tracking' or 'emissions'.
        alpha_values (tuple[float]): Parameter values to evaluate.

    Returns:
        list[dict]: List of sensitivity dispatch results.
    """
    if kind == "tracking":
        mode, objective = "tracking", "sensitivity_tracking"
    elif kind == "emissions":
        mode, objective = "bounds", "sensitivity_emissions"
    else:
        raise ValueError("kind must be 'tracking' or 'emissions'")

    points = []
    for coefficient in alpha_values:
        r = solve_model(data, mode, objective, coefficient)
        if r["status"] != "Optimal":
            raise RuntimeError(f"Sensitivity point alpha={coefficient} did not solve to optimality.")
        r["sensitivity_alpha"] = coefficient
        points.append(r)

    return points


def validate(result, data, tol=1e-4):
    """Perform rigorous, auditable physical validation checks on solved dispatch.

    Verifies:
      1. Nodal power balance residuals (< 1e-4 kW)
      2. Absence of involuntary load shedding (ENS == 0)
      3. EV departure SOC guarantee (xEV(18) >= 0.80) and post-departure disconnection
      4. Stationary battery cyclic balance (x(24) >= xin)
      5. State-of-Charge upper and lower bounds
      6. Indoor building temperature bounds [19.0, 23.0] degC
      7. Line transmission capacity limits

    Args:
        result (dict): Solved planning dispatch dictionary.
        data (dict): System parameters.
        tol (float): Numerical tolerance for floating-point equality checks.
    """
    if result["status"] != "Optimal":
        print(f"Validation skipped: {result['status']}")
        return

    mg1, mg2, mg3, sys = data["MG1"], data["MG2"], data["MG3"], data["SYS"]
    failures = 0

    def check(label, condition, detail=""):
        nonlocal failures
        ok = bool(condition)
        failures += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" ({detail})" if detail else ""))

    # Evaluate power balance residuals across all 24 hours
    residual = 0.0
    for t in range(sys["T"]):
        residual = max(
            residual,
            abs(result["fG1"][t] + result["fNR1"][t] + mg1["fR"][t] - result["fR1_curt"][t]
                + result["fS1"][t] - mg1["fL"][t] - result["f12"][t]),
            abs(mg2["fR"][t] - result["fR2_curt"][t] + result["fEV"][t] + result["f12"][t]
                - mg2["fL"][t] - result["fHP"][t] - result["f23"][t]),
            abs(result["fG3"][t] + mg3["fR"][t] - result["fR3_curt"][t]
                + result["fS3"][t] + result["f23"][t] - mg3["fL"][t]),
        )

    print("\n--- PYOMO PHYSICAL VALIDATION AUDIT ---")
    check("Hourly nodal power balances", residual <= tol, f"max residual {residual:.1e} kW")
    check("No involuntary load shedding (ENS == 0)", max(result["ENS"]) <= tol)
    check("EV SOC >= 0.80 at 18:00 departure", result["xEV"][18] >= mg2["xEVdeadline"] - tol, f"{result['xEV'][18]:.3f}")
    check("EV disconnected after 18:00", max(map(abs, result["fEV"][18:])) <= tol)
    check("Stationary BESS terminal SOC restored",
          result["x1"][-1] >= mg1["xin"] - tol and result["x3"][-1] >= mg3["xin"] - tol)
    check("SOC within physical bounds [xmin, xmax]",
          min(result["x1"]) >= mg1["xmin"] - tol and max(result["x1"]) <= mg1["xmax"] + tol
          and min(result["x3"]) >= mg3["xmin"] - tol and max(result["x3"]) <= mg3["xmax"] + tol
          and min(result["xEV"]) >= mg2["xEVmin"] - tol and max(result["xEV"]) <= mg2["xEVmax"] + tol)
    check("Building temperature in [19.0, 23.0] C",
          min(result["Tb"][1:]) >= mg2["Tmin"] - tol and max(result["Tb"][1:]) <= mg2["Tmax"] + tol,
          f"[{min(result['Tb'][1:]):.2f}, {max(result['Tb'][1:]):.2f}] C")
    check("Line-flow transmission limits",
          max(map(abs, result["f12"])) <= sys["f12max"] + tol and max(map(abs, result["f23"])) <= sys["f23max"] + tol)
    print("  => " + ("ALL AUDIT CHECKS PASSED SUCCESSFULLY" if failures == 0 else f"{failures} CHECK(S) FAILED"))


# ============================================================================
# PART 6 - VISUALIZATIONS & EXCEL REPORTING PIPELINE
# ============================================================================
def _stacked_bars(hours, components, positive, axis):
    """Helper function to render stacked bar charts for positive or negative powers."""
    bottom = np.zeros(len(hours))
    for label, values, colour in components:
        values = np.asarray(values, dtype=float)
        axis.bar(
            hours,
            values if positive else -values,
            bottom=bottom if positive else -bottom,
            width=0.78,
            label=label,
            color=colour,
        )
        bottom += values


def plot_power_balance(result, data, microgrid, filename):
    """Save an hourly stacked power-balance chart for a selected microgrid.

    Positive Bars: Power Generation / Discharging / Inflows (Supply)
    Negative Bars: Power Consumption / Charging / Outflows (Demand)

    Args:
        result (dict): Optimal dispatch solution.
        data (dict): System parameters.
        microgrid (str): 'MG1', 'MG2', or 'MG3'.
        filename (str): Destination image filename.
    """
    if result["status"] != "Optimal":
        raise ValueError("Power balances require an optimal dispatch.")

    hours = np.arange(1, 25)
    f12, f23 = np.asarray(result["f12"]), np.asarray(result["f23"])

    if microgrid == "MG1":
        positive = [
            ("fR used", np.asarray(data["MG1"]["fR"]) - result["fR1_curt"], "#5B9A68"),
            ("fG import", np.maximum(result["fG1"], 0), "#F4A261"),
            ("fNR fossil", result["fNR1"], "#B8744F"),
            ("fS discharge", np.maximum(result["fS1"], 0), "#7CB342"),
            ("MG2 to MG1", np.maximum(-f12, 0), "#455A64"),
        ]
        negative = [
            ("fL demand", data["MG1"]["fL"], "#4F81A2"),
            ("fG export", np.maximum(-np.asarray(result["fG1"]), 0), "#56B4E9"),
            ("fS charge", np.maximum(-np.asarray(result["fS1"]), 0), "#B65BAA"),
            ("MG1 to MG2", np.maximum(f12, 0), "#3D6B43"),
        ]
    elif microgrid == "MG2":
        positive = [
            ("fR used", np.asarray(data["MG2"]["fR"]) - result["fR2_curt"], "#5B9A68"),
            ("EV discharge", np.maximum(result["fEV"], 0), "#7CB342"),
            ("MG1 to MG2", np.maximum(f12, 0), "#455A64"),
            ("MG3 to MG2", np.maximum(-f23, 0), "#546E7A"),
        ]
        negative = [
            ("fL demand", data["MG2"]["fL"], "#4F81A2"),
            ("EV charge", np.maximum(-np.asarray(result["fEV"]), 0), "#B65BAA"),
            ("Heat pump", result["fHP"], "#F4A261"),
            ("MG2 to MG1", np.maximum(-f12, 0), "#3D6B43"),
            ("MG2 to MG3", np.maximum(f23, 0), "#8064A2"),
        ]
    elif microgrid == "MG3":
        positive = [
            ("fR used", np.asarray(data["MG3"]["fR"]) - result["fR3_curt"], "#5B9A68"),
            ("fG import", np.maximum(result["fG3"], 0), "#F4A261"),
            ("fS discharge", np.maximum(result["fS3"], 0), "#7CB342"),
            ("MG2 to MG3", np.maximum(f23, 0), "#455A64"),
        ]
        negative = [
            ("fL demand", data["MG3"]["fL"], "#4F81A2"),
            ("fG export", np.maximum(-np.asarray(result["fG3"]), 0), "#56B4E9"),
            ("fS charge", np.maximum(-np.asarray(result["fS3"]), 0), "#B65BAA"),
            ("MG3 to MG2", np.maximum(-f23, 0), "#3D6B43"),
        ]
    else:
        raise ValueError("microgrid must be 'MG1', 'MG2', or 'MG3'")

    fig, axis = plt.subplots(figsize=(11, 7))
    _stacked_bars(hours, positive, True, axis)
    _stacked_bars(hours, negative, False, axis)
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set(title=f"POWER BALANCE OF {microgrid}", xlabel="TIME [h]", ylabel="POWER [kW]")
    axis.set_xticks(hours)
    axis.grid(axis="y", alpha=0.3)
    axis.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.12), frameon=False)
    fig.tight_layout()

    RESULTS_DIR.mkdir(exist_ok=True)
    fig.savefig(RESULTS_DIR / filename, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_cost_emissions(points, title, ylabel, filename):
    """Plot and save a Pareto frontier curve."""
    RESULTS_DIR.mkdir(exist_ok=True)
    plt.figure(figsize=(9, 6))
    plt.plot([p["emissions_kg"] for p in points], [p["cost_for_pareto"] for p in points], "-o", markersize=5)
    plt.xlabel("CO2 emissions [kg/day]")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / filename, dpi=200)
    plt.close()


def plot_tracking_sensitivity(points):
    """Plot and save Sensitivity 1: Economic cost vs. quadratic tracking error."""
    RESULTS_DIR.mkdir(exist_ok=True)
    plt.figure(figsize=(9, 6))
    plt.plot([p["tracking_error_degC2_h"] for p in points], [p["economic_cost"] for p in points], "-o", markersize=5)
    plt.xlabel("Squared temperature-tracking error [degC^2 * h]")
    plt.ylabel("Economic cost [EUR/day]")
    plt.title("Sensitivity 1: Total cost vs. quadratic temperature-tracking error")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / "sensitivity_1_quadratic_cost_vs_tracking.png", dpi=200)
    plt.close()


def plot_emissions_sensitivity(points):
    """Plot and save Sensitivity 2: Economic cost vs. physical CO2 emissions."""
    RESULTS_DIR.mkdir(exist_ok=True)
    plt.figure(figsize=(9, 6))
    plt.plot([p["emissions_kg"] for p in points], [p["economic_cost"] for p in points], "-o", markersize=5)
    plt.xlabel("Physical CO2 emissions [kg/day]")
    plt.ylabel("Economic cost [EUR/day]")
    plt.title("Sensitivity 2: Total cost vs. CO2 emissions")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(RESULTS_DIR / "sensitivity_2_cost_vs_emissions.png", dpi=200)
    plt.close()


def export_results(planning, planning_data, pareto_tracking, pareto_bounds, tracking_sensitivity,
                   emissions_sensitivity, path=RESULTS_DIR / "results_pyomo.xlsx"):
    """Export complete baseline dispatch, both Pareto curves, and sensitivities to Excel."""
    RESULTS_DIR.mkdir(exist_ok=True)
    dispatch = pd.DataFrame({
        "hour": range(1, 25),
        "fNR1_kW": planning["fNR1"],
        "fG1_kW": planning["fG1"],
        "fS1_kW": planning["fS1"],
        "x1_end": planning["x1"][1:],
        "fR1_curt_kW": planning["fR1_curt"],
        "fEV_kW": planning["fEV"],
        "xEV_end": planning["xEV"][1:],
        "fHP_kW": planning["fHP"],
        "Tb_end_C": planning["Tb"][1:],
        "fR2_curt_kW": planning["fR2_curt"],
        "fG3_kW": planning["fG3"],
        "fS3_kW": planning["fS3"],
        "x3_end": planning["x3"][1:],
        "fR3_curt_kW": planning["fR3_curt"],
        "f12_kW": planning["f12"],
        "f23_kW": planning["f23"],
        "ENS_kW": planning["ENS"],
    }).round(3)

    def analysis_frame(points, alpha_key, include_pareto_cost=False):
        return pd.DataFrame([{
            alpha_key: point.get(alpha_key, point["alpha"]),
            "economic_cost_EUR": point["economic_cost"],
            "composite_cost_EUR": point.get("cost_for_pareto", point["economic_cost"]) if include_pareto_cost else point["economic_cost"],
            "CO2_kg": point["emissions_kg"],
            "J_track_degC2_h": point["tracking_error_degC2_h"],
        } for point in points]).round(3)

    def write_workbook(target):
        with pd.ExcelWriter(target) as writer:
            dispatch.to_excel(writer, sheet_name="planning_dispatch", index=False)
            analysis_frame(pareto_tracking, "pareto_alpha", True).to_excel(writer, sheet_name="pareto_1_tracking", index=False)
            analysis_frame(pareto_bounds, "pareto_alpha", False).to_excel(writer, sheet_name="pareto_2_bounds", index=False)
            analysis_frame(tracking_sensitivity, "alpha_T").to_excel(writer, sheet_name="tracking_sensitivity", index=False)
            analysis_frame(emissions_sensitivity, "alpha_E").to_excel(writer, sheet_name="emissions_sensitivity", index=False)

    try:
        write_workbook(path)
    except PermissionError:
        path = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        write_workbook(path)
        print("Default workbook was open; saved a timestamped copy instead.")
    print(f"Results workbook saved to {path}")


def print_results(result, title):
    """Print one valid dispatch table in the standardized project notation."""
    print(f"\n--- {title} ---\nStatus: {result['status']}")
    if result["status"] != "Optimal":
        print(result["message"])
        return
    print(f"Economic cost: EUR {result['economic_cost']:.2f} | CO2: {result['emissions_kg']:.1f} kg | "
          f"quadratic tracking error: {result['tracking_error_degC2_h']:.3f} degC^2 h | ENS: {result['ENS_kWh']:.2f} kWh")
    print(" h  fNR1    fG1    fS1    x1    fEV   xEV   fHP    Tb    fG3    fS3    x3    f12    f23    ENS")
    for t in range(24):
        print(f"{t + 1:2d} {result['fNR1'][t]:6.1f} {result['fG1'][t]:6.1f} {result['fS1'][t]:6.1f} {result['x1'][t + 1]:5.2f} "
              f"{result['fEV'][t]:6.1f} {result['xEV'][t + 1]:5.2f} {result['fHP'][t]:5.1f} {result['Tb'][t + 1]:5.2f} "
              f"{result['fG3'][t]:6.1f} {result['fS3'][t]:6.1f} {result['x3'][t + 1]:5.2f} {result['f12'][t]:6.1f} {result['f23'][t]:6.1f} {result['ENS'][t]:6.1f}")


# ============================================================================
# MAIN EXECUTION ENTRYPOINT
# ============================================================================
if __name__ == "__main__":
    print("Pyomo + Gurobi MIQP | exact quadratic temperature tracking")

    # 1. Run Diagnostic Bottleneck Evaluation
    diagnostic = solve_model(load_data(scenario="diagnostic"), objective="cost")
    print_results(diagnostic, "Diagnostic: original 200-kW grid and line limits")

    # 2. Run Feasible Planning Upgrade Scenario
    planning_data = load_data(scenario="capacity_upgrade")
    planning = solve_model(planning_data, objective="cost")
    print_results(planning, "Feasible planning scenario: 300-kW grids and 500-kW lines")

    # 3. Perform Physical Audit Validation
    validate(planning, planning_data)

    # 4. Generate & Save Power-Balance Stacked Bar Charts
    print("\n--- POWER-BALANCE FIGURES (PYOMO) ---")
    for label in ("MG1", "MG2", "MG3"):
        plot_power_balance(planning, planning_data, label, f"power_balance_{label}_pyomo.png")
        print(f"Saved {RESULTS_DIR / f'power_balance_{label}_pyomo.png'}")

    # 5. Execute Multi-Objective Pareto Frontier Analyses
    pareto_tracking = pareto_cost_emissions(planning_data, "tracking")
    pareto_bounds = pareto_cost_emissions(planning_data, "bounds")

    # 6. Execute Parametric Sensitivity Sweeps
    tracking_sensitivity = sensitivity_analysis(planning_data, "tracking")
    emissions_sensitivity = sensitivity_analysis(planning_data, "emissions")

    # 7. Print Summary Tables to Console
    print("\n--- PARETO 1: cost (energy + carbon + tracking) vs CO2 ---")
    for p in pareto_tracking:
        print(f"alpha={p['alpha']:.2f} | cost={p['cost_for_pareto']:.2f} EUR | CO2={p['emissions_kg']:.1f} kg | Jtrack={p['tracking_error_degC2_h']:.3f}")

    print("\n--- PARETO 2: cost (energy + carbon) vs CO2; 19--23 C bounds ---")
    for p in pareto_bounds:
        print(f"alpha={p['alpha']:.2f} | cost={p['cost_for_pareto']:.2f} EUR | CO2={p['emissions_kg']:.1f} kg")

    print("\n--- SENSITIVITY 1: alpha_T, cost, quadratic tracking error ---")
    for p in tracking_sensitivity:
        print(f"alpha_T={p['sensitivity_alpha']:6.2f} | cost={p['economic_cost']:.2f} EUR | Jtrack={p['tracking_error_degC2_h']:.3f}")

    print("\n--- SENSITIVITY 2: alpha_E, cost, CO2 ---")
    for p in emissions_sensitivity:
        print(f"alpha_E={p['sensitivity_alpha']:6.2f} | cost={p['economic_cost']:.2f} EUR | CO2={p['emissions_kg']:.1f} kg")

    # 8. Render & Save Pareto and Sensitivity Figures
    plot_cost_emissions(
        pareto_tracking,
        "Pareto 1: Cost (energy + carbon + tracking) vs CO2",
        "Composite cost [EUR/day]",
        "pareto_1_tracking_cost_vs_emissions.png"
    )
    plot_cost_emissions(
        pareto_bounds,
        "Pareto 2: Cost vs CO2 (hard temperature bounds)",
        "Economic cost [EUR/day]",
        "pareto_2_bounds_cost_vs_emissions.png"
    )
    plot_tracking_sensitivity(tracking_sensitivity)
    plot_emissions_sensitivity(emissions_sensitivity)

    # 9. Export All Numerical Datasets to Excel Workbook
    export_results(
        planning, planning_data, pareto_tracking, pareto_bounds,
        tracking_sensitivity, emissions_sensitivity
    )
    print(f"\nAll Pyomo figures and Excel results saved to {RESULTS_DIR}")
