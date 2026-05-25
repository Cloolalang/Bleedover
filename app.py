"""
Flask app for macro survey expected downlink calculator.
Serves a single-page UI and POST /api/expected-dl for the throughput model.
"""
# Force repo root first so we always use this package (not an installed throughput_calculator)
import sys
from pathlib import Path
_repo_root = Path(__file__).resolve().parent.parent
_repo_str = str(_repo_root)
if _repo_str not in sys.path:
    sys.path.insert(0, _repo_str)
elif sys.path[0] != _repo_str:
    sys.path.remove(_repo_str)
    sys.path.insert(0, _repo_str)

import statistics
from flask import Flask, jsonify, render_template, request

from . import throughput_model
from .power_budget_model import (
    compute_power_budget,
    normalize_carrier,
    normalize_headend,
    normalize_port,
)
from .throughput_model import (
    congestion_factor,
    effective_mcs_from_rsrp_rsrq,
    mcs_to_modulation,
    rank_factor_from_p,
    rsrq_de_rate_factor,
    rsrp_to_estimated_mcs,
    spectral_efficiency_raw_from_mcs,
    throughput_from_mcs_mbps,
)

app = Flask(__name__)


def _check_tbs_table():
    """Verify TBS table is loaded correctly; log to stderr only if something is wrong."""
    if getattr(throughput_model, "_tbs_check_done", False):
        return
    tm = throughput_model
    tbs9 = tm._mcs_to_tbs_bits(9, 100)
    tbs11 = tm._mcs_to_tbs_bits(11, 100)
    tbs28 = tm._mcs_to_tbs_bits(28, 100)
    tp9 = tm.throughput_from_mcs_mbps(9, 20.0, 0.25, rsrp_dbm=None)
    tp11 = tm.throughput_from_mcs_mbps(11, 20.0, 0.25, None)
    tp28 = tm.throughput_from_mcs_mbps(28, 20.0, 0.0, None)
    ok = (tbs9 and tbs11 and tbs28 == 75376 and tp9 > 10.0 and tp11 > 10.0 and tp28 > 75.0)
    if not ok:
        import sys
        sys.stderr.write(
            "[throughput_calculator] TBS table check failed (MCS 9/11/28). "
            "Restart from repo root: python -m throughput_calculator.app\n"
        )
        sys.stderr.flush()
    throughput_model._tbs_check_done = True


_check_tbs_table()

# UK MNO national benchmarks. Refresh when reports updated.
# List of (source_label, { MNO: speed_mbps }). Same MNO set across sources.
# Ookla: 5G/4G median (Cross-report). Opensignal: overall. RootMetrics: independent testing (UK).
BENCHMARK_SOURCES = [
    ("Ookla 5G median (H2 2025)", {
        "EE": 98,
        "Vodafone": 130,
        "Three": 214,
        "VMO2": 79,
    }),
    ("Opensignal Download Speed Experience (Jan 2026)", {
        "EE": 53.2,
        "Vodafone": 37.5,
        "Three": 51.0,
        "VMO2": 32.8,
    }),
    ("Ookla 4G median (H2 2025)", {
        "EE": 48,
        "Vodafone": 42,
        "Three": 40,
        "VMO2": 32,
    }),
    ("RootMetrics (UK)", {
        "EE": 50.0,
        "Vodafone": 38.0,
        "Three": 48.0,
        "VMO2": 30.0,
    }),
]
BENCHMARK_5G_SOURCE_LABEL = "Ookla 5G median (H2 2025)"
BENCHMARK_4G_SOURCE_LABEL = "Ookla 4G median (H2 2025)"
BENCHMARK_YEAR = "2025–2026"
BENCHMARK_SOURCE_SUMMARY = "5G: Ookla 5G median (H2 2025). Overall: Opensignal Download Speed Experience (Jan 2026), RootMetrics (UK). 4G: Ookla 4G median (H2 2025). Cross-report. Industry averages per table."


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/power-budget")
def power_budget_page():
    return render_template("power_budget.html")


@app.route("/api/power-budget", methods=["POST"])
def api_power_budget():
    """Zinwave repeater-fed DAS downlink power budget."""
    data = request.get_json(silent=True) or {}
    carriers_data = data.get("carriers") or []
    ports_data = data.get("ports") or []
    try:
        carriers = [normalize_carrier(c, i) for i, c in enumerate(carriers_data)]
        ports = [normalize_port(p, i) for i, p in enumerate(ports_data)]
        headend = normalize_headend(data.get("headend"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid power budget inputs"}), 400
    if not carriers:
        return jsonify({"error": "At least one carrier is required"}), 400
    if not ports:
        return jsonify({"error": "At least one service port is required"}), 400
    result = compute_power_budget(carriers, ports, headend)
    return jsonify(result)


@app.route("/api/expected-dl", methods=["POST"])
def api_expected_dl():
    """Expects JSON: { "carriers": [ ... ], "calibration_offset_dbm" (optional), "environment", "time_of_day", "day_of_week" }.
    Returns expected_dl_mbps (congestion-adjusted), potential_dl_mbps, calibration_offset_dbm, etc."""
    data = request.get_json(silent=True) or {}
    carriers_data = data.get("carriers") or []
    try:
        calibration_offset_dbm = float(data.get("calibration_offset_dbm", 10) or 10)
    except (TypeError, ValueError):
        calibration_offset_dbm = 10.0
    try:
        protocol_overhead_pct = float(data.get("protocol_overhead_pct", 10) or 10)
        protocol_overhead_pct = max(0.0, min(30.0, protocol_overhead_pct))
    except (TypeError, ValueError):
        protocol_overhead_pct = 10.0
    scenario_enabled = data.get("scenario_enabled", True) not in (False, "false", "0", 0)
    environment = data.get("environment", "city")
    time_of_day = data.get("time_of_day", "off_peak")
    day_of_week = data.get("day_of_week", "weekday")
    congestion_override = data.get("congestion_factor_override")
    # Functional method (Swedish Post–style): threshold in dBm; above threshold = peak/5 per carrier (no scenario, no RSRQ)
    functional_threshold_dbm = data.get("functional_threshold_dbm")
    if functional_threshold_dbm is not None and functional_threshold_dbm != "":
        try:
            functional_threshold_dbm = float(functional_threshold_dbm)
        except (TypeError, ValueError):
            functional_threshold_dbm = None
    else:
        functional_threshold_dbm = None
    try:
        carriers = []
        carrier_rats = []
        carrier_rsrq_db = []
        carrier_raw_rsrp = []  # raw (displayed) RSRP for functional method threshold comparison
        for c in carriers_data:
            raw_rsrp = c.get("rsrp_dbm", -95)
            if isinstance(raw_rsrp, str):
                raw_rsrp = raw_rsrp.strip().replace("\u2212", "-")
            rsrp = float(raw_rsrp)
            # RSRP is always negative dBm; if sent as positive (e.g. 75, 95), treat as -75, -95
            if rsrp > 0 and 50 <= rsrp <= 130:
                rsrp = -rsrp
            rsrp_effective = rsrp + calibration_offset_dbm
            bw = float(c.get("bw_mhz", 20))
            rank2_pct = c.get("rank2_pct", 25)
            try:
                rank2_fraction = max(0.0, min(1.0, float(rank2_pct) / 100.0))
            except (TypeError, ValueError):
                rank2_fraction = 0.25
            rat = (c.get("rat") or c.get("carrier_type") or "LTE").strip().upper()
            if rat not in ("LTE", "NR"):
                rat = "LTE"
            rsrq_val = c.get("rsrq_db")
            if rsrq_val is not None and rsrq_val != "":
                try:
                    # Accept number or string; normalize unicode minus (U+2212) to ASCII
                    s = str(rsrq_val).strip().replace("\u2212", "-")
                    carrier_rsrq_db.append(float(s))
                except (TypeError, ValueError):
                    carrier_rsrq_db.append(None)
            else:
                carrier_rsrq_db.append(None)
            carriers.append((rsrp_effective, bw, rank2_fraction))
            carrier_raw_rsrp.append(rsrp)
            carrier_rats.append(rat)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid carriers"}), 400
    if not carriers:
        return jsonify({
            "expected_dl_mbps": 0.0,
            "potential_dl_mbps": 0.0,
            "user_dl_mbps": 0.0,
            "user_potential_mbps": 0.0,
            "protocol_overhead_pct": round(protocol_overhead_pct, 1),
            "congestion_factor": 1.0,
            "calibration_offset_dbm": 10,
            "per_carrier_rsrq_db": [],
            "per_carrier_rsrq_de_rate_factor": [],
            "per_carrier_mbps": [],
            "per_carrier_spectral_efficiency": [],
            "effective_spectral_efficiency_mbps_per_mhz": None,
            "mno_benchmark_comparison": [],
            "benchmark_year": BENCHMARK_YEAR,
            "benchmark_source": BENCHMARK_SOURCE_SUMMARY,
            "carrier_rats": [],
            "has_nr_carriers": False,
            "per_carrier_estimated_mcs": [],
            "per_carrier_estimated_mcs_rsrp": [],
            "functional_dl_mbps": None,
            "functional_threshold_dbm": None,
        })
    # Functional method: above threshold → peak/5 (150 Mbit/s for 20 MHz 2×2 → 30 Mbit/s per 20 MHz), no scenario, no RSRQ.
    # Threshold is compared to raw (displayed) average RSRP, not calibrated. Divisor is fixed at 5 (Swedish Post style).
    PEAK_20MHZ_2X2_MBPS = 150.0
    FUNCTIONAL_DIVISOR = 5.0
    if functional_threshold_dbm is not None:
        functional_sum = 0.0
        for i, (_, bw_mhz, _) in enumerate(carriers):
            if i < len(carrier_raw_rsrp) and carrier_raw_rsrp[i] >= functional_threshold_dbm:
                functional_sum += (PEAK_20MHZ_2X2_MBPS * (bw_mhz / 20.0)) / FUNCTIONAL_DIVISOR
        functional_dl_mbps = round(functional_sum, 2)
    else:
        functional_dl_mbps = None
    # Effective MCS (RSRP+RSRQ) first, then throughput from same MCS so η and throughput stay consistent (no gaps/zeros)
    rsrq_db_per_carrier = [
        carrier_rsrq_db[i] if i < len(carrier_rsrq_db) else None
        for i in range(len(carriers))
    ]
    per_carrier_mcs = [
        effective_mcs_from_rsrp_rsrq(carriers[i][0], rsrq_db_per_carrier[i])
        for i in range(len(carriers))
    ]
    # Throughput from effective MCS only (no RSRP in fallback so legacy curve never overrides)
    per_carrier_dl = [
        throughput_from_mcs_mbps(
            per_carrier_mcs[i], carriers[i][1], carriers[i][2], rsrp_dbm=None
        )
        for i in range(len(carriers))
    ]
    total = float(sum(per_carrier_dl)) if per_carrier_dl else 0.0
    if isinstance(total, float) and total != total:
        total = 0.0
    per_carrier_rsrq_factor = [
        rsrq_de_rate_factor(carrier_rsrq_db[i] if i < len(carrier_rsrq_db) else None)
        for i in range(len(carriers))
    ]
    if congestion_override is not None and congestion_override != "":
        try:
            cf_override = float(congestion_override)
            cf = max(0.0, min(1.0, cf_override)) if isinstance(cf_override, (int, float)) else (congestion_factor(environment, time_of_day, day_of_week) if scenario_enabled else 1.0)
        except (TypeError, ValueError):
            cf = congestion_factor(environment, time_of_day, day_of_week) if scenario_enabled else 1.0
    else:
        cf = congestion_factor(environment, time_of_day, day_of_week) if scenario_enabled else 1.0
    adjusted = round(total * cf, 2) if isinstance(total, (int, float)) else 0.0
    per_carrier_estimated_mcs_rsrp = [rsrp_to_estimated_mcs(carriers[i][0]) for i in range(len(carriers))]
    per_carrier_modulation = [mcs_to_modulation(m) for m in per_carrier_mcs]
    per_carrier_se = []
    total_bw = 0.0
    weighted_eta_sum = 0.0
    for i, (rsrp, bw, rank2_fraction) in enumerate(carriers):
        # η_raw and η_eff from effective MCS (RSRP+RSRQ) so they align with displayed effective MCS
        mcs = per_carrier_mcs[i]
        eta_raw = spectral_efficiency_raw_from_mcs(mcs, rsrp_dbm=rsrp)
        eta_eff = eta_raw * rank_factor_from_p(rank2_fraction)
        per_carrier_se.append({
            "eta_raw_mbps_per_mhz": round(eta_raw, 3),
            "eta_eff_mbps_per_mhz": round(eta_eff, 3),
        })
        # Throughput must match η_eff × bw when both > 0 (never show 0 when η_eff > 0)
        if bw > 0 and eta_eff > 0:
            per_carrier_dl[i] = max(per_carrier_dl[i], eta_eff * bw)
        total_bw += bw
        weighted_eta_sum += eta_eff * bw
    effective_se = weighted_eta_sum / total_bw if total_bw else None
    total = float(sum(per_carrier_dl)) if per_carrier_dl else 0.0
    if isinstance(total, float) and total != total:
        total = 0.0
    adjusted = round(total * cf, 2) if isinstance(total, (int, float)) else 0.0

    # vs MNO national benchmarks: one row per (MNO, source). Do NOT mix 5G and overall in one "aggregated" (e.g. median of 5G + Opensignal would be misleading).
    mnos = list(BENCHMARK_SOURCES[0][1].keys()) if BENCHMARK_SOURCES else []
    sources_only = [(label, d) for label, d in BENCHMARK_SOURCES]
    mno_comparison = []
    for mno in mnos:
        for source_label, speeds in sources_only:
            n_mno = speeds.get(mno) if speeds else None
            if n_mno is None:
                continue
            pct = 100.0 * adjusted / n_mno if n_mno else 0
            mult = n_mno / adjusted if adjusted > 0 else None  # "~X× lower"
            if source_label == BENCHMARK_5G_SOURCE_LABEL:
                benchmark_type = "5g"
            elif source_label == BENCHMARK_4G_SOURCE_LABEL:
                benchmark_type = "4g"
            else:
                benchmark_type = "overall"
            mno_comparison.append({
                "mno": mno,
                "source": source_label,
                "national_benchmark_mbps": n_mno,
                "percent_of_national": round(pct, 1),
                "multiple_lower": round(mult, 1) if mult is not None and mult > 1 else None,
                "benchmark_type": benchmark_type,
            })

    # Bottom row 5G: industry average of 5G benchmark (Ookla 5G) only
    speeds_5g = BENCHMARK_SOURCES[0][1] if BENCHMARK_SOURCES else {}
    values_5g = [v for v in speeds_5g.values() if v is not None]
    if values_5g:
        industry_avg_5g = round(statistics.mean(values_5g), 1)
        pct_5g = 100.0 * adjusted / industry_avg_5g if industry_avg_5g else 0
        mult_5g = industry_avg_5g / adjusted if adjusted > 0 else None
        mno_comparison.append({
            "mno": "Industry average (5G)",
            "source": None,
            "national_benchmark_mbps": industry_avg_5g,
            "percent_of_national": round(pct_5g, 1),
            "multiple_lower": round(mult_5g, 1) if mult_5g is not None and mult_5g > 1 else None,
            "benchmark_type": "5g",
        })

    # Single industry average for 4G table: mean across all 4G/overall benchmarks (Opensignal, RootMetrics, Ookla 4G)
    sources_4g_table = [(label, d) for label, d in BENCHMARK_SOURCES if label != BENCHMARK_5G_SOURCE_LABEL]
    if sources_4g_table and mnos:
        per_mno_means = []
        for mno in mnos:
            vals = [d.get(mno) for _, d in sources_4g_table if d.get(mno) is not None]
            if vals:
                per_mno_means.append(statistics.mean(vals))
        industry_avg = round(statistics.mean(per_mno_means), 1) if per_mno_means else None
    else:
        industry_avg = None
    if industry_avg is not None:
        pct = 100.0 * adjusted / industry_avg if industry_avg else 0
        mult = industry_avg / adjusted if adjusted > 0 else None
        mno_comparison.append({
            "mno": "Industry average",
            "source": None,
            "national_benchmark_mbps": industry_avg,
            "percent_of_national": round(pct, 1),
            "multiple_lower": round(mult, 1) if mult is not None and mult > 1 else None,
            "benchmark_type": "overall",
        })

    has_nr = any(r == "NR" for r in carrier_rats)
    user_factor = 1.0 - (protocol_overhead_pct / 100.0)
    user_dl_mbps = round(adjusted * user_factor, 2) if isinstance(adjusted, (int, float)) else 0.0
    user_potential_mbps = round(total * user_factor, 2) if isinstance(total, (int, float)) else 0.0
    return jsonify(
        {
            "expected_dl_mbps": adjusted,
            "potential_dl_mbps": round(total, 2) if isinstance(total, (int, float)) else 0.0,
            "user_dl_mbps": user_dl_mbps,
            "user_potential_mbps": user_potential_mbps,
            "protocol_overhead_pct": round(protocol_overhead_pct, 1),
            "congestion_factor": round(cf, 3),
            "calibration_offset_dbm": round(calibration_offset_dbm, 1),
            "per_carrier_rsrq_db": [round(c, 1) if c is not None else None for c in carrier_rsrq_db],
            "per_carrier_rsrq_de_rate_factor": [round(f, 3) for f in per_carrier_rsrq_factor],
            "per_carrier_mbps": [round(x, 3) for x in per_carrier_dl],
            "per_carrier_spectral_efficiency": per_carrier_se,
            "effective_spectral_efficiency_mbps_per_mhz": round(effective_se, 3) if effective_se is not None else None,
            "mno_benchmark_comparison": mno_comparison,
            "benchmark_year": BENCHMARK_YEAR,
            "benchmark_source": BENCHMARK_SOURCE_SUMMARY,
            "carrier_rats": carrier_rats,
            "has_nr_carriers": has_nr,
            "per_carrier_estimated_mcs": per_carrier_mcs,
            "per_carrier_estimated_mcs_rsrp": per_carrier_estimated_mcs_rsrp,
            "per_carrier_modulation": per_carrier_modulation,
            "functional_dl_mbps": functional_dl_mbps,
            "functional_threshold_dbm": round(functional_threshold_dbm, 1) if functional_threshold_dbm is not None else None,
        }
    )


if __name__ == "__main__":
    # use_reloader=False so one process loads TBS table from repo (reloader spawns child with different cwd/path)
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
