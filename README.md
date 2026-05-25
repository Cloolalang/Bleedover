# Throughput calculator (Flask web app) — Setup & Run

## What you get
This folder contains a small Python web app:
- `app.py` (Flask server)
- `throughput_model.py` (throughput calculations)
- `power_budget_model.py` (Zinwave repeater-fed DAS downlink power budget)
- `templates/index.html` (throughput UI)
- `templates/power_budget.html` (power budget UI)
- `requirements.txt` (Python dependencies)

## Prerequisites (for the recipient)
1. Install **Python 3.10+** from https://www.python.org/
2. Ensure `pip` is available (it normally is when Python is installed).

## Setup (Windows / PowerShell)
1. Extract the zip you received somewhere, e.g. a folder like:
   - `C:\Users\<name>\Downloads\throughput_calculator_package\`
2. Open PowerShell and `cd` to the folder that contains the `throughput_calculator` directory:
   ```powershell
   cd "C:\Users\<name>\Downloads\throughput_calculator_package"
   ```
3. Install dependencies:
   ```powershell
   python -m pip install -r .\throughput_calculator\requirements.txt
   ```

## Run
Start the app:
```powershell
python -m throughput_calculator.app
```

Open in a browser:
- Throughput calculator: `http://127.0.0.1:5000`
- Zinwave power budget: `http://127.0.0.1:5000/power-budget`

## Notes / troubleshooting
- The UI loads the Mermaid diagram from a CDN, so **internet access** is needed for that diagram to render.
- If a firewall prompt appears, allow Python for local use.
- To stop the app, press `Ctrl+C` in the terminal.

