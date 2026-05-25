"""
Zinwave 5000 (UNItivity) repeater-fed DAS downlink power budget model.

Reference: zinwave.txt commissioning summaries (CP-02 repeater, CP-03 hub input,
CP-05 RU output). Computes node-by-node power for cell-idle and cell-full-load,
Element Manager settings (Calculated DL Input Level, System Active DL Gain),
and pass/fail against hardware windows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# --- Zinwave 5000 hardware limits (zinwave.txt) ---
ZINWAVE_HUB_INPUT_MIN_DBM = -5.0
ZINWAVE_HUB_INPUT_MAX_DBM = 15.0
ZINWAVE_MAX_FIBER_LINK_GAIN_DB = 25.0
ZINWAVE_RU_MAX_COMPOSITE_DBM = 20.0
# DL fiber link: optical loss (dBo) converts to RF gain reduction (1 dBo → 2 dB RF).
OPTICAL_TO_RF_DL_GAIN_FACTOR = 2.0
DEFAULT_OPTICAL_LOSS_DBO = 1.0
# PH Portal uplink balance: offset from +25 dB hub UL gain baseline (zinwave.txt).
PORTAL_UL_BALANCE_MIN_DB = -17.0   # ideal for −8 dB passive → +8 dB hub UL gain
PORTAL_UL_BALANCE_FIRMWARE_MIN_DB = -15.0  # minimum on some hub firmware builds
PORTAL_UL_BALANCE_MAX_DB = 0.0     # maximum portal setting → +25 dB hub UL gain
DEFAULT_PORTAL_UL_BALANCE_DB = -15.0  # typical field default (firmware min on many hubs)
DEFAULT_CALCULATED_DL_INPUT_LEVEL = 13  # Element Manager Calculated DL Input Level default (dBm)

# Repeater duplex feed → one RF splitter (same loss on DL and UL branch) → separate hub ports.
DEFAULT_DUPLEX_SPLITTER_LOSS_DB = 3.0
PASSIVE_LOSS_ROW_COUNT = 6
# Fixed DL path default: jumper + splitter (rows 0 & 3); rows 1–2 reserved; rows 4–5 = DL/UL attenuators.
DEFAULT_PASSIVE_LOSS_DB = 4.0
DEFAULT_PASSIVE_LOSS_ITEMS: list[dict[str, str | float | None]] = [
    {"name": "Repeater output jumper", "loss_db": 1.0},
    {"name": "", "loss_db": None},
    {"name": "", "loss_db": None},
    {"name": "Duplex RF splitter (DL & UL branch)", "loss_db": DEFAULT_DUPLEX_SPLITTER_LOSS_DB},
    {"name": "DL attenuator", "loss_db": None},
    {"name": "UL attenuator", "loss_db": None},
]


def default_passive_loss_items() -> list[PassiveLossItem]:
    return normalize_passive_loss_items(None)


# LTE macro power breathing: full load → idle drop (zinwave.txt / typical LTE).
DEFAULT_POWER_BREATHING_DB = 12.0

# UK (Ofcom IR2102) Cel-Fi repeater per-channel DL defaults — manufacturer datasheets:
# GO G41: max DL 16 dBm (UK) / 20 dBm (ROW) total; 2 relay channels → ~12 dBm/channel
#   fits 2×12 = 15 dBm composite under UK cap (Nextivity GO G41 datasheet, G41-9E-xx).
# GO G43: max 12 dBm per channel per MNO port; 17 dBm per band per port (GO G43 datasheet).
# QUATRA 4000e (EMEA/UK): 16 dBm DL per channel / per operator (QUATRA 4000 install guide + datasheet).
UK_CEL_FI_G41_FULL_DBM = 12.0
UK_CEL_FI_G41_IDLE_DBM = 0.0   # full − 12 dB breathing
UK_CEL_FI_G43_FULL_DBM = 12.0
UK_CEL_FI_G43_IDLE_DBM = 0.0
UK_CEL_FI_QUATRA_FULL_DBM = 16.0
UK_CEL_FI_QUATRA_IDLE_DBM = 4.0  # full − 12 dB breathing
# Loss when G41 and G43 RF outputs are combined before the hub feed.
DEFAULT_REPEATER_COMBINER_LOSS_DB = 0.0

DEFAULT_HEADROOM_DB = 0.0  # repeater-fed doc uses 0 dB for max coverage profile
DEFAULT_RU_MAX_COMPOSITE_DBM = 20.0
DEFAULT_GAIN_STEP_DB = 1.0

# Topology modes
TOPOLOGY_G41_G43_COMBINED = "g41_g43_combined"
TOPOLOGY_FOUR_G41_SEPARATE = "four_g41_separate_ports"
TOPOLOGY_CEL_FI_QUATRA_COMBINED = "cel_fi_quatra_combined"
TOPOLOGY_CUSTOM = "custom"
MULTI_PORT_TOPOLOGIES = frozenset({
    TOPOLOGY_FOUR_G41_SEPARATE,
})
# Cel-Fi Quatra default per-carrier DL (UK EMEA datasheet); 12 dB LTE breathing vs full load.
CEL_FI_QUATRA_FULL_DBM = UK_CEL_FI_QUATRA_FULL_DBM
CEL_FI_QUATRA_IDLE_DBM = UK_CEL_FI_QUATRA_IDLE_DBM
COMMISSIONING_PROFILES: dict[str, dict[str, float]] = {
    "repeater_fed_max": {
        "headroom_db": 0.0,
        "target_ru_composite_dbm": 20.0,
        "description": "Repeater-fed, 0 dB RU headroom (+20 dBm composite)",
    },
    "standard": {
        "headroom_db": 10.0,
        "target_ru_composite_dbm": 20.0,
        "description": "Conservative: +20 dBm RU with 10 dB cushion below +15 dBm hub ceiling",
    },
    "high_coverage": {
        "headroom_db": 5.0,
        "target_ru_composite_dbm": 15.0,
        "description": "5 dB RU headroom, +15 dBm composite target",
    },
    "maximum_coverage": {
        "headroom_db": 2.0,
        "target_ru_composite_dbm": 18.0,
        "description": "2 dB RU headroom, +18 dBm composite target",
    },
}


def dbm_to_mw(dbm: float) -> float:
    return 10.0 ** (dbm / 10.0)


def mw_to_dbm(mw: float) -> float | None:
    if mw <= 0.0:
        return None
    return 10.0 * math.log10(mw)


def sum_dbm(powers_dbm: list[float]) -> float | None:
    if not powers_dbm:
        return None
    return mw_to_dbm(sum(dbm_to_mw(p) for p in powers_dbm))


def multi_carrier_factor_db(n_carriers: int) -> float:
    """Power added when n equal carriers combine: 10*log10(n)."""
    if n_carriers <= 0:
        return 0.0
    return 10.0 * math.log10(n_carriers)


def per_carrier_from_composite(composite_dbm: float, n_carriers: int) -> float | None:
    if n_carriers <= 0:
        return None
    return composite_dbm - multi_carrier_factor_db(n_carriers)


def rsrp_normalized_20mhz(channel_power_dbm: float) -> float:
    """RSRP for 20 MHz LTE: channel power − 10*log10(1200) ≈ −30.8 dB."""
    return channel_power_dbm - 10.0 * math.log10(1200)


def round_db(value: float | None, places: int = 1) -> float | None:
    if value is None:
        return None
    return round(value, places)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def snap_gain(gain_db: float, step_db: float = DEFAULT_GAIN_STEP_DB) -> float:
    if step_db <= 0:
        return gain_db
    return round(gain_db / step_db) * step_db


@dataclass
class CarrierInput:
    carrier_id: str
    label: str
    repeater: str  # G41 | G43 | QUATRA (model)
    mno: str
    dl_power_idle_dbm: float
    dl_power_full_dbm: float
    repeater_unit_id: str = "g41-1"  # g41-1..g41-4, g43-1
    service_port_id: str = "sp1"
    active_idle: bool = True
    active_full: bool = True


@dataclass
class PassiveLossItem:
    name: str
    loss_db: float


@dataclass
class HeadendConfig:
    passive_loss_db: float = DEFAULT_PASSIVE_LOSS_DB
    passive_loss_items: list[PassiveLossItem] | None = None
    repeater_combiner_loss_db: float = DEFAULT_REPEATER_COMBINER_LOSS_DB
    headroom_db: float = DEFAULT_HEADROOM_DB
    target_ru_composite_dbm: float = DEFAULT_RU_MAX_COMPOSITE_DBM
    hub_input_min_dbm: float = ZINWAVE_HUB_INPUT_MIN_DBM
    hub_input_max_dbm: float = ZINWAVE_HUB_INPUT_MAX_DBM
    max_system_gain_db: float = ZINWAVE_MAX_FIBER_LINK_GAIN_DB
    ru_max_composite_dbm: float = ZINWAVE_RU_MAX_COMPOSITE_DBM
    gain_step_db: float = DEFAULT_GAIN_STEP_DB
    power_breathing_db: float = DEFAULT_POWER_BREATHING_DB
    profile: str = "repeater_fed_max"
    topology: str = TOPOLOGY_CEL_FI_QUATRA_COMBINED
    optical_loss_dbo: float = DEFAULT_OPTICAL_LOSS_DBO


@dataclass
class ServicePortInput:
    """Zinwave service module port — one DL input with optional dedicated repeater feed(s)."""
    port_id: str
    name: str
    fed_by_repeater_units: list[str] | None = None
    passive_loss_items: list[PassiveLossItem] | None = None
    passive_loss_db: float | None = None
    repeater_combiner_loss_db: float = 0.0
    path_loss_db: float = 0.0
    current_system_gain_db: float | None = None
    calculated_dl_input_level: int | None = None
    dl_agc_db: float | None = None
    ul_attenuation_db: float | None = None
    portal_ul_balance_db: float | None = None
    antenna_gain_dbi: float = 0.0
    eirp_limit_dbm: float | None = None


def total_passive_loss_db(items: list[PassiveLossItem]) -> float:
    return sum(i.loss_db for i in items)


def _passive_row_loss(items: list[PassiveLossItem], index: int) -> float:
    if index >= len(items):
        return 0.0
    return max(0.0, items[index].loss_db)


def passive_chain_breakdown(items: list[PassiveLossItem]) -> dict[str, float]:
    """
    Passive row layout (6 rows):
      0 jumper · 1–2 reserved (optional coax/combiner) · 3 splitter · 4 DL atten · 5 UL atten.
    """
    padded = list(items)
    while len(padded) < PASSIVE_LOSS_ROW_COUNT:
        d = DEFAULT_PASSIVE_LOSS_ITEMS[len(padded)]
        loss = d.get("loss_db")
        padded.append(
            PassiveLossItem(str(d.get("name") or ""), 0.0 if loss is None else float(loss))
        )
    pre_splitter = sum(_passive_row_loss(padded, i) for i in range(3))
    splitter_db = _passive_row_loss(padded, 3)
    dl_attenuator_db = _passive_row_loss(padded, 4)
    ul_attenuator_db = _passive_row_loss(padded, 5)
    passive_before_atten = pre_splitter + splitter_db
    return {
        "pre_splitter_coax_db": pre_splitter,
        "duplex_splitter_db": splitter_db,
        "duplex_splitter_dl_db": splitter_db,
        "duplex_splitter_ul_db": splitter_db,
        "dl_attenuator_db": dl_attenuator_db,
        "ul_attenuator_db": ul_attenuator_db,
        "passive_before_dl_atten_db": passive_before_atten,
        "total_passive_excl_atten_db": passive_before_atten,
        "ul_coax_shared_db": pre_splitter,
    }


def merge_passive_attenuators(
    items: list[PassiveLossItem],
    dl_attenuation_db: float,
    ul_attenuation_db: float,
) -> list[PassiveLossItem]:
    """Return passive rows with DL/UL attenuator values filled in for display/API."""
    out = list(items)
    while len(out) < PASSIVE_LOSS_ROW_COUNT:
        d = DEFAULT_PASSIVE_LOSS_ITEMS[len(out)]
        loss = d.get("loss_db")
        out.append(
            PassiveLossItem(str(d.get("name") or ""), 0.0 if loss is None else float(loss))
        )
    names = [i.name for i in out[:PASSIVE_LOSS_ROW_COUNT]]
    if not names[4] or names[4].strip() == "":
        names[4] = "DL attenuator"
    if not names[5] or names[5].strip() == "":
        names[5] = "UL attenuator"
    merged: list[PassiveLossItem] = []
    for idx in range(PASSIVE_LOSS_ROW_COUNT):
        if idx == 4:
            merged.append(PassiveLossItem(names[4], dl_attenuation_db))
        elif idx == 5:
            merged.append(PassiveLossItem(names[5], ul_attenuation_db))
        else:
            merged.append(PassiveLossItem(names[idx], out[idx].loss_db))
    return merged


def normalize_passive_loss_items(raw_items: list[Any] | None) -> list[PassiveLossItem]:
    if not raw_items:
        return [
            PassiveLossItem(
                str(d.get("name") or ""),
                0.0 if d.get("loss_db") is None else float(d["loss_db"]),
            )
            for d in DEFAULT_PASSIVE_LOSS_ITEMS
        ]
    out: list[PassiveLossItem] = []
    for i, row in enumerate(raw_items):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("element") or "").strip()
        raw_loss = row.get("loss_db")
        if raw_loss is None or raw_loss == "":
            loss = 0.0
        else:
            try:
                loss = max(0.0, float(raw_loss))
            except (TypeError, ValueError):
                loss = 0.0
        out.append(PassiveLossItem(name, loss))
    while len(out) < PASSIVE_LOSS_ROW_COUNT:
        d = DEFAULT_PASSIVE_LOSS_ITEMS[len(out)]
        loss = d.get("loss_db")
        out.append(
            PassiveLossItem(
                str(d.get("name") or ""),
                0.0 if loss is None else float(loss),
            )
        )
    return out[:PASSIVE_LOSS_ROW_COUNT]


def passive_loss_for_port(
    port: ServicePortInput,
    global_headend: HeadendConfig,
) -> tuple[float, float, list[PassiveLossItem]]:
    """Passive loss for one service module port path."""
    if port.passive_loss_items:
        items = port.passive_loss_items
        return total_passive_loss_db(items), port.repeater_combiner_loss_db, items
    if port.passive_loss_db is not None:
        return port.passive_loss_db, port.repeater_combiner_loss_db, []
    return passive_loss_for_hub(global_headend)


def passive_loss_for_hub(headend: HeadendConfig) -> tuple[float, float, list[PassiveLossItem]]:
    """
    (passive_loss_db, repeater_combiner_loss_db, items) for hub power calculation.
    When line items are set, their sum replaces passive_loss_db.
    """
    if headend.passive_loss_items:
        items = headend.passive_loss_items
        return total_passive_loss_db(items), headend.repeater_combiner_loss_db, items
    return headend.passive_loss_db, headend.repeater_combiner_loss_db, []


def hub_power_per_carrier(
    carrier: CarrierInput,
    *,
    scenario: str,
    passive_before_atten_db: float,
    repeater_combiner_loss_db: float,
    dl_attenuation_db: float = 0.0,
) -> float | None:
    if scenario == "idle":
        if not carrier.active_idle:
            return None
        p_rep = carrier.dl_power_idle_dbm
    elif scenario == "full":
        if not carrier.active_full:
            return None
        p_rep = carrier.dl_power_full_dbm
    else:
        raise ValueError(scenario)
    return p_rep - passive_before_atten_db - dl_attenuation_db - repeater_combiner_loss_db


def ru_power_per_carrier(p_hub_dbm: float, system_gain_db: float, path_loss_db: float) -> float:
    return p_hub_dbm + system_gain_db - path_loss_db


def system_gain_from_targets(
    hub_composite_full_dbm: float,
    target_ru_composite_dbm: float,
    max_gain_db: float,
    path_loss_db: float = 0.0,
) -> float:
    """Gain so RU composite (full load) = target: gain = target − hub + path_loss."""
    required = target_ru_composite_dbm - hub_composite_full_dbm + path_loss_db
    return clamp(required, 0.0, max_gain_db)


def design_ru_composite_dbm(headend: HeadendConfig) -> float:
    """Operating target at RU, never above hardware ceiling (+20 dBm default)."""
    desired = headend.target_ru_composite_dbm - headend.headroom_db
    return min(desired, headend.ru_max_composite_dbm)


def max_gain_for_ru_composite_cap(
    hub_composite_dbm: float,
    ru_cap_dbm: float,
    path_loss_db: float,
    max_gain_db: float,
) -> float:
    """Highest system gain that keeps full-load RU composite at or below ru_cap_dbm."""
    return system_gain_from_targets(hub_composite_dbm, ru_cap_dbm, max_gain_db, path_loss_db)


def power_label(n_carriers: int) -> str:
    """User-facing label: composite power when n>1 carriers, else power."""
    return "composite power" if n_carriers > 1 else "power"


def calculated_dl_input_level(hub_composite_full_dbm: float) -> int:
    """Round protected hub full-load composite to integer dBm (commissioning reference)."""
    return int(round(hub_composite_full_dbm))


def resolve_calculated_dl_input_level(port: ServicePortInput) -> int:
    if port.calculated_dl_input_level is not None:
        return port.calculated_dl_input_level
    return DEFAULT_CALCULATED_DL_INPUT_LEVEL


def optical_rf_dl_loss_db(optical_loss_dbo: float) -> float:
    """RF gain reduction from hub→RU optical link loss (1 dBo = 2 dB RF on DL)."""
    return max(0.0, optical_loss_dbo) * OPTICAL_TO_RF_DL_GAIN_FACTOR


def effective_max_dl_fiber_gain_db(
    optical_loss_dbo: float = DEFAULT_OPTICAL_LOSS_DBO,
) -> float:
    """Max DL fiber link RF gain after optical loss (default 1 dBo → 23 dB RF)."""
    return clamp(
        ZINWAVE_MAX_FIBER_LINK_GAIN_DB - optical_rf_dl_loss_db(optical_loss_dbo),
        0.0,
        ZINWAVE_MAX_FIBER_LINK_GAIN_DB,
    )


def element_manager_dl_gain_budget_db(
    calculated_dl_input_level: float,
    max_fiber_gain_db: float = ZINWAVE_MAX_FIBER_LINK_GAIN_DB,
) -> float:
    """EM internal DL gain budget: max fiber RF gain minus Calc DL Input Level."""
    return clamp(
        max_fiber_gain_db - calculated_dl_input_level,
        0.0,
        max_fiber_gain_db,
    )


def element_manager_dl_gain_db(
    calculated_dl_input_level: float,
    hub_composite_dbm: float,
    ru_target_dbm: float,
    path_loss_db: float = 0.0,
    max_fiber_gain_db: float = ZINWAVE_MAX_FIBER_LINK_GAIN_DB,
) -> float:
    """
    Locked System Active DL Gain in Element Manager.

    min(effective max DL gain − Calculated DL Input Level, gain to hit RU target).
    """
    em_budget = element_manager_dl_gain_budget_db(calculated_dl_input_level, max_fiber_gain_db)
    ru_sized = system_gain_from_targets(
        hub_composite_dbm,
        ru_target_dbm,
        max_fiber_gain_db,
        path_loss_db,
    )
    return min(em_budget, ru_sized)


def ul_hub_gain_from_portal_balance(portal_balance_db: float) -> float:
    """Hub UL amplifier gain from PH Portal balance offset (0 dB → +25 dB)."""
    return ZINWAVE_MAX_FIBER_LINK_GAIN_DB + portal_balance_db


def net_ul_system_gain_db(ul_hub_gain_db: float, total_ul_passive_db: float) -> float:
    """Net RU → repeater UL gain after hub amplification and coax/splitter/attenuator loss."""
    return ul_hub_gain_db - total_ul_passive_db


def recommended_portal_ul_balance_db(total_ul_passive_db: float) -> float:
    """Portal balance for unity net UL: hub UL gain equals path loss to repeater."""
    target = total_ul_passive_db - ZINWAVE_MAX_FIBER_LINK_GAIN_DB
    return clamp(target, PORTAL_UL_BALANCE_MIN_DB, PORTAL_UL_BALANCE_MAX_DB)


def resolve_portal_ul_balance_db(port: ServicePortInput) -> float:
    if port.portal_ul_balance_db is not None:
        return port.portal_ul_balance_db
    return DEFAULT_PORTAL_UL_BALANCE_DB


def recommended_dl_agc_db(raw_hub_full_dbm: float | None, hub_max_dbm: float) -> float:
    """DL AGC / external attenuation so full-load hub input does not exceed hub_max_dbm."""
    if raw_hub_full_dbm is None:
        return 0.0
    return max(0.0, raw_hub_full_dbm - hub_max_dbm)


def protected_hub_input_dbm(raw_hub_dbm: float | None, dl_agc_db: float) -> float | None:
    if raw_hub_dbm is None:
        return None
    return raw_hub_dbm - dl_agc_db


def port_headend_config(global_headend: HeadendConfig, port: ServicePortInput) -> HeadendConfig:
    passive_db, combiner_db, items = passive_loss_for_port(port, global_headend)
    return HeadendConfig(
        passive_loss_db=passive_db,
        passive_loss_items=items if items else None,
        repeater_combiner_loss_db=combiner_db,
        headroom_db=global_headend.headroom_db,
        target_ru_composite_dbm=global_headend.target_ru_composite_dbm,
        hub_input_min_dbm=global_headend.hub_input_min_dbm,
        hub_input_max_dbm=global_headend.hub_input_max_dbm,
        max_system_gain_db=global_headend.max_system_gain_db,
        ru_max_composite_dbm=global_headend.ru_max_composite_dbm,
        gain_step_db=global_headend.gain_step_db,
        power_breathing_db=global_headend.power_breathing_db,
        profile=global_headend.profile,
        topology=global_headend.topology,
        optical_loss_dbo=global_headend.optical_loss_dbo,
    )


def carriers_for_port(
    carriers: list[CarrierInput],
    port: ServicePortInput,
) -> list[CarrierInput]:
    """Carriers assigned to this service module port (by service_port_id, then fed_by filter)."""
    pid = port.port_id
    port_carriers = [c for c in carriers if c.service_port_id == pid]
    fed = port.fed_by_repeater_units
    if not fed:
        return port_carriers
    fed_set = set(fed)
    return [c for c in port_carriers if c.repeater_unit_id in fed_set]


def default_carriers_g41_g43() -> list[CarrierInput]:
    g41_full, g41_idle = UK_CEL_FI_G41_FULL_DBM, UK_CEL_FI_G41_IDLE_DBM
    g43_full, g43_idle = UK_CEL_FI_G43_FULL_DBM, UK_CEL_FI_G43_IDLE_DBM
    return [
        CarrierInput("g41-1", "G41 Radio 1 — LTE B3", "G41", "MNO A", g41_idle, g41_full, "g41-1", "sp1"),
        CarrierInput("g41-2", "G41 Radio 2 — LTE B20", "G41", "MNO A", g41_idle, g41_full, "g41-1", "sp1"),
        CarrierInput("g43-b1", "G43 MNO B — Radio 1 B3", "G43", "MNO B", g43_idle, g43_full, "g43-1", "sp1"),
        CarrierInput("g43-b2", "G43 MNO B — Radio 2 B1", "G43", "MNO B", g43_idle, g43_full, "g43-1", "sp1"),
        CarrierInput("g43-c1", "G43 MNO C — Radio 1 B3", "G43", "MNO C", g43_idle, g43_full, "g43-1", "sp1"),
        CarrierInput("g43-c2", "G43 MNO C — Radio 2 B20", "G43", "MNO C", g43_idle, g43_full, "g43-1", "sp1"),
        CarrierInput("g43-d1", "G43 MNO D — Radio 1 B3", "G43", "MNO D", g43_idle, g43_full, "g43-1", "sp1"),
        CarrierInput("g43-d2", "G43 MNO D — Radio 2 B7", "G43", "MNO D", g43_idle, g43_full, "g43-1", "sp1"),
    ]


def default_carriers_four_g41() -> list[CarrierInput]:
    """4× G41 repeaters, 2 radios each (1 MNO per repeater)."""
    g_full, g_idle = UK_CEL_FI_G41_FULL_DBM, UK_CEL_FI_G41_IDLE_DBM
    mnos = ["MNO A", "MNO B", "MNO C", "MNO D"]
    carriers: list[CarrierInput] = []
    for n in range(1, 5):
        uid = f"g41-{n}"
        sp = f"sp{n}"
        carriers.append(
            CarrierInput(f"{uid}-r1", f"G41-{n} Radio 1 — LTE B3", "G41", mnos[n - 1], g_idle, g_full, uid, sp)
        )
        carriers.append(
            CarrierInput(f"{uid}-r2", f"G41-{n} Radio 2 — LTE B20", "G41", mnos[n - 1], g_idle, g_full, uid, sp)
        )
    return carriers


def default_carriers_cel_fi_quatra() -> list[CarrierInput]:
    """Cel-Fi Quatra: 4 MNO ports × 2 radios (8 total), single DAS port."""
    g_full = CEL_FI_QUATRA_FULL_DBM
    g_idle = CEL_FI_QUATRA_IDLE_DBM
    mnos = ["MNO A", "MNO B", "MNO C", "MNO D"]
    suffixes = ["a", "b", "c", "d"]
    carriers: list[CarrierInput] = []
    for i, mno in enumerate(mnos):
        sp = "sp1"
        letter = mno.split()[-1]
        carriers.append(
            CarrierInput(
                f"quatra-{suffixes[i]}r1",
                f"Quatra MNO {letter} — Radio 1",
                "QUATRA",
                mno,
                g_idle,
                g_full,
                "quatra-1",
                sp,
            )
        )
        carriers.append(
            CarrierInput(
                f"quatra-{suffixes[i]}r2",
                f"Quatra MNO {letter} — Radio 2",
                "QUATRA",
                mno,
                g_idle,
                g_full,
                "quatra-1",
                sp,
            )
        )
    return carriers


def default_ports_combined() -> list[ServicePortInput]:
    return [
        ServicePortInput(
            port_id="sp1",
            name="Service module port 1 (G41 + G43 combined)",
            fed_by_repeater_units=["g41-1", "g43-1"],
            passive_loss_items=default_passive_loss_items(),
        ),
    ]


def default_ports_four_g41_separate() -> list[ServicePortInput]:
    ports: list[ServicePortInput] = []
    for n in range(1, 5):
        items = default_passive_loss_items()
        ports.append(
            ServicePortInput(
                port_id=f"sp{n}",
                name=f"Service module port {n} ← G41-{n}",
                fed_by_repeater_units=[f"g41-{n}"],
                passive_loss_items=items,
            )
        )
    return ports


def default_ports_cel_fi_quatra_combined() -> list[ServicePortInput]:
    return [
        ServicePortInput(
            port_id="sp1",
            name="Service module port 1 (Cel-Fi Quatra — all MNO outputs combined)",
            fed_by_repeater_units=["quatra-1"],
            passive_loss_items=default_passive_loss_items(),
        ),
    ]


def _active_carriers(carriers: list[CarrierInput], scenario: str) -> list[CarrierInput]:
    if scenario == "idle":
        return [c for c in carriers if c.active_idle]
    return [c for c in carriers if c.active_full]


def _repeater_unit_composites(
    carriers: list[CarrierInput],
    scenario: str,
) -> dict[str, float | None]:
    units: dict[str, list[float]] = {}
    for c in carriers:
        if scenario == "idle" and not c.active_idle:
            continue
        if scenario == "full" and not c.active_full:
            continue
        p = c.dl_power_idle_dbm if scenario == "idle" else c.dl_power_full_dbm
        units.setdefault(c.repeater_unit_id, []).append(p)
    return {uid: round_db(sum_dbm(powers)) for uid, powers in units.items()}


def _repeater_composites(carriers: list[CarrierInput], scenario: str) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for rep in sorted(set(c.repeater for c in carriers)):
        powers = [
            (c.dl_power_idle_dbm if scenario == "idle" else c.dl_power_full_dbm)
            for c in carriers
            if c.repeater == rep and (c.active_idle if scenario == "idle" else c.active_full)
        ]
        out[rep] = round_db(sum_dbm(powers))
    return out


def _carrier_rows(
    carriers: list[CarrierInput],
    scenario: str,
    headend: HeadendConfig,
    system_gain_db: float,
    path_loss_db: float,
    dl_attenuation_db: float = 0.0,
    *,
    passive_before_atten_db: float | None = None,
    repeater_combiner_loss_db: float | None = None,
) -> list[dict[str, Any]]:
    passive_db, combiner_db, items = passive_loss_for_hub(headend)
    combiner_db = repeater_combiner_loss_db if repeater_combiner_loss_db is not None else combiner_db
    if passive_before_atten_db is None:
        chain = passive_chain_breakdown(items if items else [])
        passive_before_atten_db = (
            chain["passive_before_dl_atten_db"] if items else passive_db
        )
    rows = []
    for c in carriers:
        p_rep = c.dl_power_idle_dbm if scenario == "idle" else c.dl_power_full_dbm
        p_hub = hub_power_per_carrier(
            c,
            scenario=scenario,
            passive_before_atten_db=passive_before_atten_db,
            repeater_combiner_loss_db=combiner_db,
            dl_attenuation_db=dl_attenuation_db,
        )
        if p_hub is None:
            rows.append({
                "carrier_id": c.carrier_id,
                "label": c.label,
                "repeater": c.repeater,
                "mno": c.mno,
                "active": False,
                "repeater_dl_dbm": None,
                "hub_dl_dbm": None,
                "ru_dl_dbm": None,
                "rsrp_20mhz_dbm": None,
            })
            continue
        p_ru = ru_power_per_carrier(p_hub, system_gain_db, path_loss_db)
        rows.append({
            "carrier_id": c.carrier_id,
            "label": c.label,
            "repeater": c.repeater,
            "mno": c.mno,
            "active": True,
            "repeater_dl_dbm": round_db(p_rep),
            "hub_dl_dbm": round_db(p_hub),
            "ru_dl_dbm": round_db(p_ru),
            "rsrp_20mhz_dbm": round_db(rsrp_normalized_20mhz(p_ru)),
        })
    return rows


def _build_scenario(
    carriers: list[CarrierInput],
    scenario: str,
    n_active: int,
    mcf: float,
    hub_comp: float | None,
    headend: HeadendConfig,
    system_gain_db: float,
    path_loss_db: float,
    ru_cap_dbm: float,
    dl_attenuation_db: float = 0.0,
    *,
    passive_before_atten_db: float | None = None,
    repeater_combiner_loss_db: float | None = None,
) -> dict[str, Any]:
    rep_composites = _repeater_unit_composites(carriers, scenario)
    rep_by_model = _repeater_composites(carriers, scenario)
    rep_all = [
        (c.dl_power_idle_dbm if scenario == "idle" else c.dl_power_full_dbm)
        for c in _active_carriers(carriers, scenario)
    ]
    repeater_composite = sum_dbm(rep_all)

    rows = _carrier_rows(
        carriers,
        scenario,
        headend,
        system_gain_db,
        path_loss_db,
        dl_attenuation_db,
        passive_before_atten_db=passive_before_atten_db,
        repeater_combiner_loss_db=repeater_combiner_loss_db,
    )
    ru_powers = [r["ru_dl_dbm"] for r in rows if r["active"] and r["ru_dl_dbm"] is not None]
    composite_ru = sum_dbm(ru_powers)
    strongest_ru = max(ru_powers) if ru_powers else None
    weakest_ru = min(ru_powers) if ru_powers else None

    hub_rounded = round_db(hub_comp, 1) if hub_comp is not None else None
    hub_ok = (
        hub_rounded is not None
        and headend.hub_input_min_dbm <= hub_rounded <= headend.hub_input_max_dbm
    )
    ru_over = round_db(composite_ru - ru_cap_dbm) if composite_ru is not None else None
    hub_under = (
        round_db(headend.hub_input_min_dbm - hub_comp)
        if hub_comp is not None and hub_comp < headend.hub_input_min_dbm
        else None
    )
    hub_over = (
        round_db(hub_comp - headend.hub_input_max_dbm)
        if hub_comp is not None and hub_comp > headend.hub_input_max_dbm
        else None
    )

    return {
        "carriers": rows,
        "repeater_composite_dbm": round_db(repeater_composite),
        "repeater_by_unit": rep_composites,
        "repeater_by_model": rep_by_model,
        "hub_composite_dbm": round_db(hub_comp),
        "ru_composite_dbm": round_db(composite_ru),
        "strongest_ru_dbm": round_db(strongest_ru),
        "weakest_ru_dbm": round_db(weakest_ru),
        "multi_carrier_factor_db": round_db(mcf, 1),
        "per_carrier_expected_dbm": round_db(
            per_carrier_from_composite(composite_ru, n_active) if composite_ru and n_active else None
        ),
        "hub_within_window": hub_ok,
        "hub_under_db": hub_under,
        "hub_over_db": hub_over,
        "ru_over_db": ru_over,
        "ru_at_cap": ru_over is None or ru_over <= 0,
        "pass": (
            hub_ok
            and (ru_over is None or ru_over <= 0)
            and (hub_under is None or hub_under <= 0)
            and (hub_over is None or hub_over <= 0)
        ),
    }


def compute_port_budget(
    carriers: list[CarrierInput],
    port: ServicePortInput,
    global_headend: HeadendConfig,
) -> dict[str, Any]:
    port_carriers = carriers_for_port(carriers, port)
    headend = port_headend_config(global_headend, port)
    n_full = len(_active_carriers(port_carriers, "full"))
    n_idle = len(_active_carriers(port_carriers, "idle"))
    mcf_full = multi_carrier_factor_db(n_full)
    mcf_idle = multi_carrier_factor_db(n_idle)
    passive_db, combiner_db, p_items = passive_loss_for_port(port, global_headend)
    chain = passive_chain_breakdown(p_items if p_items else [])
    passive_before_atten = (
        chain["passive_before_dl_atten_db"]
        if p_items
        else passive_db
    )

    hub_full_powers_raw = [
        hub_power_per_carrier(
            c, scenario="full",
            passive_before_atten_db=passive_before_atten,
            repeater_combiner_loss_db=combiner_db,
        )
        for c in _active_carriers(port_carriers, "full")
    ]
    hub_full_powers_raw = [p for p in hub_full_powers_raw if p is not None]
    hub_idle_powers_raw = [
        hub_power_per_carrier(
            c, scenario="idle",
            passive_before_atten_db=passive_before_atten,
            repeater_combiner_loss_db=combiner_db,
        )
        for c in _active_carriers(port_carriers, "idle")
    ]
    hub_idle_powers_raw = [p for p in hub_idle_powers_raw if p is not None]

    hub_composite_full = sum_dbm(hub_full_powers_raw)
    hub_composite_idle = sum_dbm(hub_idle_powers_raw)

    rep_full_powers = [
        c.dl_power_full_dbm
        for c in _active_carriers(port_carriers, "full")
    ]
    rep_idle_powers = [
        c.dl_power_idle_dbm
        for c in _active_carriers(port_carriers, "idle")
    ]
    repeater_composite_full = sum_dbm(rep_full_powers)
    repeater_composite_idle = sum_dbm(rep_idle_powers)

    hub_max = headend.hub_input_max_dbm
    rec_agc = recommended_dl_agc_db(hub_composite_full, hub_max)
    dl_from_row = chain["dl_attenuator_db"]
    ul_from_row = chain["ul_attenuator_db"]
    if port.dl_agc_db is not None:
        dl_attenuation = port.dl_agc_db
    elif dl_from_row > 0:
        dl_attenuation = dl_from_row
    else:
        dl_attenuation = rec_agc
    if port.ul_attenuation_db is not None:
        ul_attenuation = port.ul_attenuation_db
    elif ul_from_row > 0:
        ul_attenuation = ul_from_row
    else:
        ul_attenuation = dl_attenuation if dl_attenuation > 0 else 0.0

    display_items = merge_passive_attenuators(
        p_items if p_items else [], dl_attenuation, ul_attenuation
    )

    hub_full_powers = [
        hub_power_per_carrier(
            c, scenario="full",
            passive_before_atten_db=passive_before_atten,
            repeater_combiner_loss_db=combiner_db,
            dl_attenuation_db=dl_attenuation,
        )
        for c in _active_carriers(port_carriers, "full")
    ]
    hub_full_powers = [p for p in hub_full_powers if p is not None]
    hub_idle_powers = [
        hub_power_per_carrier(
            c, scenario="idle",
            passive_before_atten_db=passive_before_atten,
            repeater_combiner_loss_db=combiner_db,
            dl_attenuation_db=dl_attenuation,
        )
        for c in _active_carriers(port_carriers, "idle")
    ]
    hub_idle_powers = [p for p in hub_idle_powers if p is not None]
    protected_hub_full = sum_dbm(hub_full_powers)
    protected_hub_idle = sum_dbm(hub_idle_powers)

    calc_dl_input = resolve_calculated_dl_input_level(port)

    ru_cap = headend.ru_max_composite_dbm
    design_ru = design_ru_composite_dbm(headend)
    hub_full_val = protected_hub_full if protected_hub_full is not None else 0.0
    optical_loss_dbo = headend.optical_loss_dbo
    optical_rf_loss = optical_rf_dl_loss_db(optical_loss_dbo)
    effective_max_gain = effective_max_dl_fiber_gain_db(optical_loss_dbo)

    ru_sized_gain = max_gain_for_ru_composite_cap(
        hub_full_val,
        design_ru,
        port.path_loss_db,
        effective_max_gain,
    )
    if calc_dl_input is not None:
        em_gain_budget = element_manager_dl_gain_budget_db(calc_dl_input, effective_max_gain)
        recommended_gain = min(em_gain_budget, ru_sized_gain)
    else:
        em_gain_budget = None
        recommended_gain = ru_sized_gain
    recommended_gain = snap_gain(recommended_gain, headend.gain_step_db)

    current_gain = port.current_system_gain_db
    design_gain = recommended_gain

    # Design matrix: gain sized to hit RU composite cap (~+20 dBm at full load)
    scenario_kw = {
        "passive_before_atten_db": passive_before_atten,
        "repeater_combiner_loss_db": combiner_db,
    }
    design_idle = _build_scenario(
        port_carriers, "idle", n_idle, mcf_idle, protected_hub_idle,
        headend, design_gain, port.path_loss_db, ru_cap, dl_attenuation,
        **scenario_kw,
    )
    design_full = _build_scenario(
        port_carriers, "full", n_full, mcf_full, protected_hub_full,
        headend, design_gain, port.path_loss_db, ru_cap, dl_attenuation,
        **scenario_kw,
    )

    current_full = None
    current_idle = None
    if current_gain is not None:
        current_full = _build_scenario(
            port_carriers, "full", n_full, mcf_full, protected_hub_full,
            headend, current_gain, port.path_loss_db, ru_cap, dl_attenuation,
            **scenario_kw,
        )
        current_idle = _build_scenario(
            port_carriers, "idle", n_idle, mcf_idle, protected_hub_idle,
            headend, current_gain, port.path_loss_db, ru_cap, dl_attenuation,
            **scenario_kw,
        )

    cw_walk_offset_db = round_db(-mcf_full, 1) if n_full else None
    rsrp_walk_offset_db = round_db(-(mcf_full + 10 * math.log10(1200)), 1) if n_full else None

    current_over = (
        current_full is not None
        and current_full.get("ru_over_db") is not None
        and current_full["ru_over_db"] > 0
    )

    p_total = passive_before_atten + dl_attenuation + combiner_db
    hub_excess = (
        round_db(max(0.0, hub_composite_full - hub_max))
        if hub_composite_full is not None
        else None
    )
    raw_attenuation_needed = hub_excess is not None and hub_excess > 0
    remaining_atten_db = round_db(max(0.0, rec_agc - dl_attenuation), 1)
    protected_rounded = (
        round_db(protected_hub_full, 1) if protected_hub_full is not None else None
    )
    within_hub_window = (
        protected_rounded is not None
        and headend.hub_input_min_dbm <= protected_rounded <= hub_max
    )
    attenuation_still_required = remaining_atten_db > 0
    ul_total_passive = (
        chain["ul_coax_shared_db"]
        + chain["duplex_splitter_ul_db"]
        + ul_attenuation
    )
    rec_portal_balance = recommended_portal_ul_balance_db(ul_total_passive)
    portal_balance = resolve_portal_ul_balance_db(port)
    ul_hub_gain = ul_hub_gain_from_portal_balance(portal_balance)
    net_ul_gain = net_ul_system_gain_db(ul_hub_gain, ul_total_passive)

    dl_path_loss_db = passive_before_atten + dl_attenuation + combiner_db

    return {
        "port_id": port.port_id,
        "name": port.name,
        "fed_by_repeater_units": port.fed_by_repeater_units or [],
        "dl_path_loss_db": round_db(dl_path_loss_db, 2),
        "ul_path_loss_db": round_db(ul_total_passive, 2),
        "passive_loss_total_db": round_db(dl_path_loss_db, 2),
        "passive_loss_items": [
            {"name": i.name, "loss_db": round_db(i.loss_db, 2) if i.loss_db else None}
            for i in display_items
        ],
        "passive_chain": {k: round_db(v, 2) for k, v in chain.items()},
        "active_carrier_count_full": n_full,
        "active_carrier_count_idle": n_idle,
        "power_label_full": power_label(n_full),
        "power_label_idle": power_label(n_idle),
        "path_loss_db": port.path_loss_db,
        "ru_composite_cap_dbm": round_db(ru_cap),
        "design_ru_composite_dbm": round_db(design_ru),
        "current_system_gain_db": round_db(current_gain) if current_gain is not None else None,
        "recommended_system_gain_db": round_db(recommended_gain),
        "em_dl_gain_budget_db": round_db(em_gain_budget) if em_gain_budget is not None else None,
        "ru_sized_system_gain_db": round_db(ru_sized_gain),
        "optical_loss_dbo": round_db(optical_loss_dbo, 2),
        "optical_rf_dl_loss_db": round_db(optical_rf_loss, 2),
        "effective_max_dl_gain_db": round_db(effective_max_gain, 1),
        "gain_delta_db": (
            round_db(recommended_gain - current_gain)
            if current_gain is not None
            else None
        ),
        "calculated_dl_input_level": calc_dl_input,
        "dl_agc_db": round_db(dl_attenuation, 1),
        "recommended_dl_agc_db": round_db(rec_agc, 1),
        "ul_attenuation_db": round_db(ul_attenuation, 1),
        "recommended_ul_attenuation_db": round_db(dl_attenuation if rec_agc > 0 else 0.0, 1),
        "portal_ul_balance_db": round_db(portal_balance, 1),
        "recommended_portal_ul_balance_db": round_db(rec_portal_balance, 1),
        "ul_hub_gain_db": round_db(ul_hub_gain, 1),
        "net_ul_system_gain_db": round_db(net_ul_gain, 1),
        "das_dl_service_port": {
            "repeater_composite_full_dbm": round_db(repeater_composite_full),
            "repeater_composite_idle_dbm": round_db(repeater_composite_idle),
            "pre_splitter_coax_db": round_db(chain["pre_splitter_coax_db"], 2),
            "duplex_splitter_dl_db": round_db(chain["duplex_splitter_dl_db"], 2),
            "dl_attenuator_db": round_db(dl_attenuation, 1),
            "patch_after_dl_atten_db": None,
            "passive_loss_total_db": round_db(p_total, 2),
            "raw_dl_input_full_dbm": round_db(hub_composite_full),
            "raw_dl_input_idle_dbm": round_db(hub_composite_idle),
            "hub_input_max_dbm": hub_max,
            "hub_input_excess_db": hub_excess,
            "attenuation_required": raw_attenuation_needed,
            "attenuation_still_required": attenuation_still_required,
            "required_external_attenuation_db": round_db(rec_agc, 1),
            "remaining_external_attenuation_db": remaining_atten_db,
            "installed_external_attenuation_db": round_db(dl_attenuation, 1),
            "dl_agc_db": round_db(dl_attenuation, 1),
            "recommended_dl_agc_db": round_db(rec_agc, 1),
            "protected_dl_input_full_dbm": round_db(protected_hub_full),
            "calculated_dl_input_level": calc_dl_input,
            "within_hub_window": within_hub_window,
            "signal_path": (
                "Repeater (duplex) → coax → RF splitter [−"
                f"{chain['duplex_splitter_db']:.0f} dB] → DL attenuator [−{dl_attenuation:.1f} dB] "
                f"→ patch → Zinwave DL port"
            ),
        },
        "das_ul_service_port": {
            "duplex_splitter_ul_db": round_db(chain["duplex_splitter_ul_db"], 2),
            "ul_coax_shared_db": round_db(chain["ul_coax_shared_db"], 2),
            "ul_attenuator_db": round_db(ul_attenuation, 1),
            "recommended_ul_attenuation_db": round_db(dl_attenuation if rec_agc > 0 else 0.0, 1),
            "total_ul_passive_db": round_db(ul_total_passive, 2),
            "portal_ul_balance_db": round_db(portal_balance, 1),
            "recommended_portal_ul_balance_db": round_db(rec_portal_balance, 1),
            "ul_hub_gain_db": round_db(ul_hub_gain, 1),
            "net_ul_system_gain_db": round_db(net_ul_gain, 1),
            "unity_gain_target_db": 0.0,
            "at_unity_gain": abs(net_ul_gain) < 0.55,
            "firmware_balance_below_min": portal_balance < PORTAL_UL_BALANCE_FIRMWARE_MIN_DB,
            "signal_path": (
                "RU → hub UL amp [+"
                f"{ul_hub_gain:.0f} dB] → UL attenuator [−{ul_attenuation:.1f} dB] → RF splitter [−"
                f"{chain['duplex_splitter_db']:.0f} dB] → coax (duplex) → repeater"
            ),
        },
        "idle": design_idle,
        "full": design_full,
        "current_idle": current_idle,
        "current_full": current_full,
        "current_exceeds_ru_cap": current_over,
        "verification_matrix": {
            "full_load": {
                "repeater_out_dbm": design_full["repeater_composite_dbm"],
                "hub_input_dbm": design_full["hub_composite_dbm"],
                "ru_output_dbm": design_full["ru_composite_dbm"],
            },
            "idle": {
                "repeater_out_dbm": design_idle["repeater_composite_dbm"],
                "hub_input_dbm": design_idle["hub_composite_dbm"],
                "ru_output_dbm": design_idle["ru_composite_dbm"],
            },
        },
        "walk_test": {
            "cw_plotting_offset_db": cw_walk_offset_db,
            "rsrp_20mhz_plotting_offset_db": rsrp_walk_offset_db,
        },
        "overall_pass": (
            n_full > 0 and design_idle["pass"] and design_full["pass"]
        ),
    }


def compute_power_budget(
    carriers: list[CarrierInput],
    ports: list[ServicePortInput],
    headend: HeadendConfig | None = None,
) -> dict[str, Any]:
    headend = headend or HeadendConfig()
    port_results = [compute_port_budget(carriers, p, headend) for p in ports]
    multi_port = len(ports) > 1 or headend.topology in MULTI_PORT_TOPOLOGIES
    primary = port_results[0] if port_results else None
    passive_db, combiner_db, passive_items = passive_loss_for_hub(headend)
    passive_total = passive_db + combiner_db

    all_units: dict[str, dict[str, float | None]] = {}
    for sc in ("idle", "full"):
        all_units[sc] = _repeater_unit_composites(carriers, sc)
    repeater_full = _repeater_composites(carriers, "full")
    repeater_idle = _repeater_composites(carriers, "idle")
    repeater_models = sorted(set(c.repeater for c in carriers))

    return {
        "platform": "Zinwave 5000 (UNItivity)",
        "topology": headend.topology,
        "multi_port": multi_port,
        "profile": headend.profile,
        "profile_description": COMMISSIONING_PROFILES.get(headend.profile, {}).get("description"),
        "headend": {
            "passive_loss_db": round_db(passive_db, 2),
            "passive_loss_total_db": round_db(passive_total, 2),
            "passive_loss_items": [
                {"name": item.name, "loss_db": round_db(item.loss_db, 2)}
                for item in passive_items
            ],
            "repeater_combiner_loss_db": combiner_db,
            "headroom_db": headend.headroom_db,
            "target_ru_composite_dbm": headend.target_ru_composite_dbm,
            "hub_input_window_dbm": [headend.hub_input_min_dbm, headend.hub_input_max_dbm],
            "max_system_gain_db": headend.max_system_gain_db,
            "ru_max_composite_dbm": headend.ru_max_composite_dbm,
            "power_breathing_db": headend.power_breathing_db,
            "active_carrier_count_full": len(_active_carriers(carriers, "full")),
            "active_carrier_count_idle": len(_active_carriers(carriers, "idle")),
            "multi_carrier_factor_full_db": round_db(
                multi_carrier_factor_db(len(_active_carriers(carriers, "full"))), 1
            ),
            "optical_loss_dbo": round_db(headend.optical_loss_dbo, 2),
            "optical_rf_dl_loss_db": round_db(optical_rf_dl_loss_db(headend.optical_loss_dbo), 2),
            "effective_max_dl_gain_db": round_db(
                effective_max_dl_fiber_gain_db(headend.optical_loss_dbo), 1
            ),
        },
        "element_manager": {
            "calculated_dl_input_level": primary["calculated_dl_input_level"] if primary and not multi_port else None,
            "dl_agc_db": primary["dl_agc_db"] if primary and not multi_port else None,
            "system_active_dl_gain_db": primary["recommended_system_gain_db"] if primary and not multi_port else None,
            "em_dl_gain_budget_db": primary["em_dl_gain_budget_db"] if primary and not multi_port else None,
            "ru_sized_system_gain_db": primary["ru_sized_system_gain_db"] if primary and not multi_port else None,
            "optical_loss_dbo": round_db(headend.optical_loss_dbo, 2) if primary and not multi_port else None,
            "effective_max_dl_gain_db": primary["effective_max_dl_gain_db"] if primary and not multi_port else None,
            "ru_composite_cap_dbm": primary["ru_composite_cap_dbm"] if primary else ZINWAVE_RU_MAX_COMPOSITE_DBM,
            "note": (
                "Per-port Element Manager settings below when using separate service module ports. "
                f"Calculated DL Input Level defaults to +{DEFAULT_CALCULATED_DL_INPUT_LEVEL} dBm "
                "(adjust to match protected full-load hub composite at the DL port after DL attenuator). "
                "DL optical loss (dBo) reduces max fiber gain by 2 dB RF per 1 dBo. "
                "System Active DL Gain = min(effective max − Calc DL Input Level, gain sized for RU ≤ "
                f"+{headend.ru_max_composite_dbm:.0f} dBm). Enable DL attenuator when raw input "
                f"exceeds +{headend.hub_input_max_dbm:.0f} dBm so protected hub input stays within "
                f"{headend.hub_input_min_dbm:.0f} to +{headend.hub_input_max_dbm:.0f} dBm."
            ),
        },
        "repeater_units": all_units,
        "repeaters": {
            rep: {"full": repeater_full.get(rep), "idle": repeater_idle.get(rep)}
            for rep in repeater_models
        },
        "carriers": [
            {
                "carrier_id": c.carrier_id,
                "label": c.label,
                "repeater": c.repeater,
                "repeater_unit_id": c.repeater_unit_id,
                "service_port_id": c.service_port_id,
                "mno": c.mno,
                "dl_power_idle_dbm": c.dl_power_idle_dbm,
                "dl_power_full_dbm": c.dl_power_full_dbm,
                "active_idle": c.active_idle,
                "active_full": c.active_full,
            }
            for c in carriers
        ],
        "ports": port_results,
        "all_ports_pass": all(p["overall_pass"] for p in port_results) if port_results else True,
    }


def apply_profile(headend: HeadendConfig, profile: str) -> HeadendConfig:
    preset = COMMISSIONING_PROFILES.get(profile)
    if not preset:
        return headend
    headend.profile = profile
    headend.headroom_db = preset["headroom_db"]
    headend.target_ru_composite_dbm = preset["target_ru_composite_dbm"]
    return headend


def normalize_carrier(raw: dict[str, Any], index: int) -> CarrierInput:
    radio_on = raw.get("radio_on")
    if radio_on is not None:
        on = radio_on not in (False, "false", "0", 0)
        active_idle = active_full = on
    else:
        active_idle = raw.get("active_idle", True) not in (False, "false", "0", 0)
        active_full = raw.get("active_full", True) not in (False, "false", "0", 0)
    return CarrierInput(
        carrier_id=str(raw.get("carrier_id") or f"carrier-{index}"),
        label=str(raw.get("label") or f"Carrier {index + 1}"),
        repeater=str(raw.get("repeater") or "G41").upper(),
        mno=str(raw.get("mno") or "—"),
        dl_power_idle_dbm=float(raw.get("dl_power_idle_dbm", -4.8)),
        dl_power_full_dbm=float(raw.get("dl_power_full_dbm", 7.2)),
        repeater_unit_id=str(raw.get("repeater_unit_id") or raw.get("repeater_unit") or "g41-1"),
        service_port_id=str(raw.get("service_port_id") or "sp1"),
        active_idle=active_idle,
        active_full=active_full,
    )


def normalize_port(raw: dict[str, Any], index: int) -> ServicePortInput:
    eirp = raw.get("eirp_limit_dbm")
    gain = raw.get("current_system_gain_db", raw.get("current_gain_db"))
    fed = raw.get("fed_by_repeater_units") or raw.get("fed_by") or None
    if fed is not None and not isinstance(fed, list):
        fed = [str(fed)]
    pl_items = raw.get("passive_loss_items")
    return ServicePortInput(
        port_id=str(raw.get("port_id") or f"port-{index}"),
        name=str(raw.get("name") or f"Service module port {index + 1}"),
        fed_by_repeater_units=[str(x) for x in fed] if fed else None,
        passive_loss_items=normalize_passive_loss_items(pl_items) if pl_items else None,
        passive_loss_db=(
            float(raw["passive_loss_db"])
            if raw.get("passive_loss_db") not in (None, "")
            else None
        ),
        repeater_combiner_loss_db=float(raw.get("repeater_combiner_loss_db", 0) or 0),
        path_loss_db=float(raw.get("path_loss_db", 0)),
        current_system_gain_db=float(gain) if gain is not None and gain != "" else None,
        calculated_dl_input_level=(
            int(raw["calculated_dl_input_level"])
            if raw.get("calculated_dl_input_level") not in (None, "")
            else None
        ),
        dl_agc_db=(
            float(raw["dl_agc_db"])
            if raw.get("dl_agc_db") not in (None, "")
            else None
        ),
        ul_attenuation_db=(
            float(raw["ul_attenuation_db"])
            if raw.get("ul_attenuation_db") not in (None, "")
            else None
        ),
        portal_ul_balance_db=(
            float(raw["portal_ul_balance_db"])
            if raw.get("portal_ul_balance_db") not in (None, "")
            else DEFAULT_PORTAL_UL_BALANCE_DB
        ),
        antenna_gain_dbi=float(raw.get("antenna_gain_dbi", 0)),
        eirp_limit_dbm=float(eirp) if eirp is not None and eirp != "" else None,
    )


def normalize_headend(raw: dict[str, Any] | None) -> HeadendConfig:
    raw = raw or {}
    items = normalize_passive_loss_items(raw.get("passive_loss_items"))
    if raw.get("passive_loss_items") is not None:
        passive_db = total_passive_loss_db(items)
    elif raw.get("passive_loss_db") not in (None, ""):
        passive_db = float(raw["passive_loss_db"])
        items = []
    else:
        passive_db = total_passive_loss_db(items)

    headend = HeadendConfig(
        passive_loss_db=passive_db,
        passive_loss_items=items if items else None,
        repeater_combiner_loss_db=float(
            raw.get("repeater_combiner_loss_db", DEFAULT_REPEATER_COMBINER_LOSS_DB)
        ),
        headroom_db=float(raw.get("headroom_db", DEFAULT_HEADROOM_DB)),
        target_ru_composite_dbm=min(
            float(raw.get("target_ru_composite_dbm", DEFAULT_RU_MAX_COMPOSITE_DBM)),
            float(raw.get("ru_max_composite_dbm", ZINWAVE_RU_MAX_COMPOSITE_DBM)),
        ),
        hub_input_min_dbm=float(raw.get("hub_input_min_dbm", ZINWAVE_HUB_INPUT_MIN_DBM)),
        hub_input_max_dbm=float(raw.get("hub_input_max_dbm", ZINWAVE_HUB_INPUT_MAX_DBM)),
        max_system_gain_db=float(raw.get("max_system_gain_db", ZINWAVE_MAX_FIBER_LINK_GAIN_DB)),
        ru_max_composite_dbm=float(raw.get("ru_max_composite_dbm", ZINWAVE_RU_MAX_COMPOSITE_DBM)),
        gain_step_db=float(raw.get("gain_step_db", DEFAULT_GAIN_STEP_DB)),
        power_breathing_db=float(raw.get("power_breathing_db", DEFAULT_POWER_BREATHING_DB)),
        profile=str(raw.get("profile") or "repeater_fed_max"),
        topology=str(raw.get("topology") or TOPOLOGY_CEL_FI_QUATRA_COMBINED),
        optical_loss_dbo=float(raw.get("optical_loss_dbo", DEFAULT_OPTICAL_LOSS_DBO)),
    )
    profile = raw.get("profile")
    if profile and profile in COMMISSIONING_PROFILES:
        apply_profile(headend, profile)
        if raw.get("headroom_db") not in (None, ""):
            headend.headroom_db = float(raw["headroom_db"])
        if raw.get("target_ru_composite_dbm") not in (None, ""):
            headend.target_ru_composite_dbm = min(
                float(raw["target_ru_composite_dbm"]),
                headend.ru_max_composite_dbm,
            )
    headend.target_ru_composite_dbm = min(
        headend.target_ru_composite_dbm,
        headend.ru_max_composite_dbm,
    )
    if not headend.passive_loss_items and items:
        headend.passive_loss_items = items
    return headend
