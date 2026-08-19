# Assumption Register - Three-Microgrid Project

This register supplements `data_m.xlsx`.  It is the numerical authority for
values not supplied by the workbook or the 2025 project brief.  The values are
assumptions for a deterministic 24-hour day-ahead model and must be reported
with the final results.

## Adopted assumptions

| Item | Adopted value | Unit | Rationale |
|---|---:|---|---|
| Time step | 1 | h | Project data are hourly. |
| MG2 direct grid connection | Not allowed | - | Matches the project topology: MG2 exchanges with MG1 and MG3 only. [Project brief, slide 10](2025_Microgrid_Project.pptx) |
| Interconnection capacities | 200 for MG1-MG2; 200 for MG2-MG3 | kW | **Scenario assumption, not a measured rating.** Equal to the workbook grid limit, so a line never exceeds the demonstrated external-connection capability. Test 100/300 kW in sensitivity runs. |
| Interconnection losses | 0 | % | A deliberate lossless-network simplification for the base course model. |
| MG1 fossil technology | Natural-gas generator | - | Course examples use gas generation. |
| Fossil generation cost | 0.10 | EUR/kWh_e | Course demand-response example uses `Cgas = 0.1`; replaces the workbook's unlabeled `Cnr = 10`. |
| Grid CO2 factor | 0.1752 | kgCO2/kWh_e | ISPRA's latest final Italian electricity-*consumption* factor: 175.2 gCO2/kWh in 2024, including grid losses and imports. This is preferable to a global average for a University of Genoa project. [ISPRA 2026, Table 1.8](https://www.isprambiente.gov.it/files2026/pubblicazioni/rapporti/r430-2026-2.pdf) |
| Gas-generator CO2 factor | 0.3706 | kgCO2/kWh_e | ISPRA's 2024 Italian factor for electricity generated from natural gas: 370.6 gCO2/kWh. It avoids assuming an unprovided generator efficiency. [ISPRA 2026, Table 1.8](https://www.isprambiente.gov.it/files2026/pubblicazioni/rapporti/r430-2026-2.pdf) |
| Carbon price | 0.03 | EUR/kgCO2 | Equals 30 EUR/tCO2, specified in the project brief. [Project brief, slide 10](2025_Microgrid_Project.pptx) |
| Stationary battery self-discharge multiplier | 1.00 | - | **24-hour modelling assumption.** No self-discharge is applied over one day; the workbook charge/discharge efficiencies still capture conversion loss. |
| MG1/MG3 terminal SOC | equal to initial SOC | - | Prevents end-of-horizon battery depletion from artificially lowering cost; terminal-SOC requirements are standard in 24-hour EV/microgrid scheduling. [Zhang et al. (2025)](https://www.mdpi.com/2227-9717/13/11/3421) |
| MG2 stationary battery | None | - | The project brief lists an EV, but no stationary MG2 battery. The workbook's 500 kWh capacity is therefore assigned to the EV. [Project brief, slide 10](2025_Microgrid_Project.pptx) |
| EV capacity | 500 | kWh | Workbook value. |
| EV initial SOC | 0.20 | - | Workbook value. |
| EV target SOC | >= 0.80 at start of hour 18 | - | Workbook target. |
| EV availability | Hours 8-17 | - | **Scenario assumption.** A 10-hour workplace connection window permits the required 300 kWh SOC increase with the cited 50 kW charger: 10 x 50 x 0.85 = 425 kWh available, versus 300 kWh required. EV availability must be explicit because optimization research treats arrival/departure preferences as key inputs. [Salvatti et al. (2020)](https://www.mdpi.com/1996-1073/13/5/1191) |
| EV G2V / V2G limits | 50 / 50 | kW | The course EV slide lists 50 kW CCS/CHAdeMO fast charging; recent V2G research identifies 10-60 kW as the demonstrated bidirectional-charger range and adopts 50 kW as a mid-scale rating. [Course EV slides, slide 5](../3_EVs_events.pptx); [Alruwaili et al. (2026)](https://www.mdpi.com/2227-7080/14/3/185) |
| EV after departure | G2V = V2G = 0 for hours 18-24 | kW | The target at hour 18 represents departure. |
| Heat-pump maximum electrical power | 30 | kW_e | **Derived design assumption.** At the coldest supplied temperature (276 K), a 30 kW heat pump supplies 30 x 1.8 = 54 kW_th. With the brief's C^B = 50 kWh/K and R^ext = 400 K/kW, this restores a building at 20 C to the 21 C target in about 0.93 h with no occupants. It is therefore sufficient but not arbitrarily oversized. [Project brief, slide 11](2025_Microgrid_Project.pptx); [calculation below](#heat-pump-check) |
| Initial indoor temperature | 293.15 | K | **20 C**, the lower edge of the adopted occupied comfort band. This models a credible cool-start while avoiding the unjustified assumption that the building begins exactly at its 21 C target. A published thermal experiment also initializes indoor air at 20 C. [Park et al. (2022)](https://www.mdpi.com/2071-1050/14/22/15127) |
| Hard comfort band | 293.15 to 297.15 | K | **20-24 C.** This is a published indoor-comfort range used in integrated-building optimization; the project target of 21 C lies within it. [Sun et al. (2025)](https://www.mdpi.com/2075-5309/15/13/2294) |
| Thermal capacity / resistance / EER | 50 / 400 / 1.8 | kWh/K, K/kW, - | Values supplied by the project slide. [Project brief, slide 11](2025_Microgrid_Project.pptx) |
| Internal heat gains | 0.1 kW/person; 25 people in hours 8-13 and 14-16 | kW | Values supplied by the project slide. [Project brief, slide 11](2025_Microgrid_Project.pptx) |
| Deferrable MG2 energy | 100 | kWh/day | **Scenario assumption.** It is 4.8% of the 2,083 kWh fixed MG2 daily demand in `data_m.xlsx`, large enough to show demand response without dominating the microgrid. |
| Deferrable-load window | Hours 8-18 | - | **Scenario assumption.** A daytime service window is consistent with the project building's occupancy pattern; it leaves 11 one-hour periods for scheduling. |
| Deferrable-load power limit | 20 | kW | **Derived scenario value.** It makes the 100 kWh task require at least 5 h, creating genuine scheduling flexibility without exceeding 13.8% of MG2's 145 kW peak fixed demand. |
| Deferrable-load unmet-energy penalty | 10 | EUR/kWh | Directly follows the professor's demand-response example (`Cud = 10`), making unmet flexible energy economically unattractive. [Course demand-response examples, example 6](Demand%20response%20examples%20exam.pdf) |
| Renewable curtailment | Allowed, penalized only by forgone sale revenue | kW | Guarantees feasibility if a local/export/storage limit binds; interconnected-microgrid research also retains curtailment as a last-resort feasibility measure. [Kourtis et al. (2025)](https://www.mdpi.com/1996-1073/18/8/2087) |
| Fixed-load curtailment | Not allowed | - | **Scope decision.** The project defines these as fixed electrical loads; load shedding would require a new value-of-lost-load parameter that is not provided. |
| Pareto methodology | Normalized weighted sum | - | Lecturer-confirmed method. Use w = 0, 0.1, ..., 1 after separately computing the individual-objective minima and maxima; published EV-microgrid work uses the same scalar weighted-sum form. [Zhang et al. (2025)](https://www.mdpi.com/2227-9717/13/11/3421) |

## Objective definitions

For the cost-emissions frontier, solve:

`min w * normalized_cost + (1-w) * normalized_emissions`

For the cost-comfort frontier, solve:

`min w * normalized_cost + (1-w) * normalized_tracking_error`

The normalization avoids combining quantities with incompatible units.  In the
cost-emissions runs, temperature remains inside the hard 20-24 C comfort band.

## Heat-pump check

At the coldest supplied external temperature, `T_ext = 276 K`, with `T_in =
293.15 K`, no occupants, and the heating convention
`T_in(t+1) = T_in(t) + (Q_HP + Q_int + Q_ext) * dt / C^B`:

`Q_ext = (276 - 293.15) / 400 = -0.0429 kW_th`

`Q_HP = 30 * 1.8 = 54 kW_th`

`delta_T = (54 - 0.0429) / 50 = 1.079 K/h`

Thus a 30 kW electrical limit can recover the 1 K gap to the 21 C target
within one one-hour interval.  The previous 80 kW proposal is withdrawn.

## Sources

- Course/project brief: `2025_Microgrid_Project.pptx`, slides 10-11.
- Course demand-response examples: `Demand response examples exam.pdf`, examples 4, 6, 8 and 10.
- ISPRA, *Settore elettrico: emissioni di CO2 e altri impatti*, 2026, Table 1.8: https://www.isprambiente.gov.it/files2026/pubblicazioni/rapporti/r430-2026-2.pdf
- IPCC, *2006 Guidelines, Volume 2 Energy*, Table 1.4: https://www.ipcc-nggip.iges.or.jp/public/2006gl/pdf/2_Volume2/V2_1_Ch1_Introduction.pdf
- *Integration of heat production and thermal comfort models in microgrid operation planning*, Energy Procedia (2018): https://www.sciencedirect.com/science/article/abs/pii/S2352467718300444
- Kourtis et al., *Effective and Local Constraint-Aware Load Shifting for Microgrid-Based Energy Communities*, Energies (2025): https://www.mdpi.com/1996-1073/18/2/343
