# Multi-Microgrid Energy Optimization for Sustainable Energy Management

## Overview

This repository contains a reproducible Python redevelopment of a graduate
energy-engineering optimisation project at the University of Genoa. The model
optimises the 24-hour operation of three interconnected microgrids integrating
renewables, stationary batteries, an EV, a heat pump, building thermal
dynamics, fossil generation, and external-grid interaction.

## Objectives

- Minimise operating and carbon costs.
- Minimise physical CO2 emissions.
- Coordinate battery, EV, heat-pump, grid, and inter-microgrid flows.
- Analyse cost-emissions and cost-comfort trade-offs.

## Current Status

The 24-hour Python/PuLP model is implemented and verified. It includes a
feasibility diagnosis, optimal dispatch, power-balance visualisations, a
normalised cost-emissions Pareto front, and temperature- and emissions-weight
sensitivity analyses.

## Repository Structure

```text
data/          # Course input workbook
docs/          # Project brief and assumptions register
images/        # Generated power-balance, Pareto, and sensitivity figures
results/       # Exported Excel results workbook
src/           # Reproducible Python/PuLP model
legacy-lingo/  # Placeholder for the original LINGO implementation
```

## Running the model

From the `src` directory, install the dependencies listed in `pyproject.toml`
and run:

```bash
uv run python main.py
```

The script reads `data/data_m.xlsx`, writes figures to `images/`, and prints
the dispatch, Pareto, and sensitivity tables. The complete exported workbook
is available in `results/`.

## Skills Demonstrated

- Energy-systems optimisation
- Smart grids and distributed energy resources
- Battery storage and V2G scheduling
- Building thermal modelling
- Multi-objective optimisation and sensitivity analysis
- Python, PuLP, and data visualisation
