"""Tests for Zinwave 5000 downlink power budget model."""
from throughput_calculator.power_budget_model import (
    CarrierInput,
    HeadendConfig,
    ServicePortInput,
    calculated_dl_input_level,
    compute_power_budget,
    default_carriers_g41_g43,
    multi_carrier_factor_db,
    normalize_passive_loss_items,
    power_label,
    rsrp_normalized_20mhz,
    sum_dbm,
    system_gain_from_targets,
    total_passive_loss_db,
)


def test_sum_dbm_two_equal():
    assert abs(sum_dbm([0.0, 0.0]) - 3.0) < 0.05


def test_multi_carrier_factor_six():
    assert abs(multi_carrier_factor_db(6) - 7.8) < 0.1


def test_passive_loss_items_sum():
    items = normalize_passive_loss_items([
        {"name": "A", "loss_db": 2},
        {"name": "B", "loss_db": 3},
    ])
    assert len(items) == 6
    assert items[0].loss_db == 2.0
    assert items[1].loss_db == 3.0
    assert items[3].name.lower().find("splitter") >= 0


def test_repeater_fed_reference_values():
    """Match zinwave.txt repeater-fed matrix: +15→+7 hub, gain 13→+20 RU."""
    carriers = [
        CarrierInput(f"c{i}", f"C{i}", "G43", "MNO", -4.8, 7.2)
        for i in range(6)
    ]
    headend = HeadendConfig(
        passive_loss_db=8.0,
        headroom_db=0.0,
        target_ru_composite_dbm=20.0,
    )
    ports = [ServicePortInput("sp1", "Ref RU", current_system_gain_db=13.0)]
    result = compute_power_budget(carriers, ports, headend)
    port = result["ports"][0]
    vm = port["verification_matrix"]
    assert abs(vm["full_load"]["repeater_out_dbm"] - 15.0) < 0.5
    assert abs(vm["full_load"]["hub_input_dbm"] - 7.0) < 0.5
    assert abs(vm["full_load"]["ru_output_dbm"] - 17.0) < 0.5
    assert calculated_dl_input_level(7.0) == 7
    assert port["calculated_dl_input_level"] == 13
    assert port["recommended_system_gain_db"] == 10.0


def test_system_gain_from_targets():
    assert system_gain_from_targets(7.0, 20.0, 25.0) == 13.0
    assert system_gain_from_targets(7.0, 20.0, 25.0, path_loss_db=2.0) == 15.0


def test_element_manager_dl_gain_from_calc_input():
    from throughput_calculator.power_budget_model import (
        effective_max_dl_fiber_gain_db,
        element_manager_dl_gain_db,
        optical_rf_dl_loss_db,
    )

    assert optical_rf_dl_loss_db(1.0) == 2.0
    assert effective_max_dl_fiber_gain_db(1.0) == 23.0
    assert effective_max_dl_fiber_gain_db(0.0) == 25.0

    # Calc matches hub: 25 − 7 = 18 budget, RU cap sizes to 13 dB
    assert element_manager_dl_gain_db(7, 7.0, 20.0) == 13.0
    # Calc 0: full 25 dB budget, still capped by RU target
    assert element_manager_dl_gain_db(0, 7.0, 20.0) == 13.0
    assert element_manager_dl_gain_db(0, 5.0, 15.0) == 10.0
    # Higher calc reduces EM budget below RU cap
    assert element_manager_dl_gain_db(15, 7.0, 20.0) == 10.0
    assert element_manager_dl_gain_db(0, 5.0, 20.0) == 15.0
    # 1 dBo optical loss → 23 dB effective max
    assert element_manager_dl_gain_db(15, 7.0, 20.0, max_fiber_gain_db=23.0) == 8.0


def test_optical_loss_reduces_locked_dl_gain():
    from throughput_calculator.power_budget_model import (
        CarrierInput,
        HeadendConfig,
        ServicePortInput,
        compute_power_budget,
    )

    carriers = [
        CarrierInput(f"c{i}", f"C{i}", "G43", "MNO", -4.8, 7.2)
        for i in range(6)
    ]
    ports = [ServicePortInput("sp1", "Ref", calculated_dl_input_level=12)]
    headend_kw = {"passive_loss_db": 8.0, "headroom_db": 0.0}
    no_optical = compute_power_budget(
        carriers, ports, HeadendConfig(**headend_kw, optical_loss_dbo=0.0)
    )
    with_optical = compute_power_budget(
        carriers, ports, HeadendConfig(**headend_kw, optical_loss_dbo=1.0)
    )
    assert no_optical["ports"][0]["recommended_system_gain_db"] == 13.0
    assert with_optical["ports"][0]["recommended_system_gain_db"] == 11.0
    assert with_optical["ports"][0]["effective_max_dl_gain_db"] == 23.0
    assert with_optical["ports"][0]["optical_rf_dl_loss_db"] == 2.0


def test_rsrp_normalization():
    assert abs(rsrp_normalized_20mhz(12.2) - (-18.6)) < 0.2


def test_radios_off_reduce_composite():
    carriers = [
        CarrierInput(f"c{i}", f"C{i}", "G43", "MNO", -4.8, 7.2)
        for i in range(6)
    ]
    carriers[0].active_full = carriers[0].active_idle = False
    carriers[1].active_full = carriers[1].active_idle = False
    headend = HeadendConfig(passive_loss_db=8.0, headroom_db=0.0)
    full = compute_power_budget(carriers, [ServicePortInput("sp1", "Ref")], headend)
    all_on = compute_power_budget(
        [CarrierInput(f"c{i}", f"C{i}", "G43", "MNO", -4.8, 7.2) for i in range(6)],
        [ServicePortInput("sp1", "Ref")],
        headend,
    )
    assert full["headend"]["active_carrier_count_full"] == 4
    assert all_on["headend"]["active_carrier_count_full"] == 6
    hub_4 = full["ports"][0]["full"]["hub_composite_dbm"]
    hub_6 = all_on["ports"][0]["full"]["hub_composite_dbm"]
    assert hub_4 is not None and hub_6 is not None
    assert hub_4 < hub_6


def test_idle_full_with_g41_g43_defaults():
    carriers = default_carriers_g41_g43()
    result = compute_power_budget(
        carriers,
        [ServicePortInput("sp1", "Ref")],
        HeadendConfig(passive_loss_db=8.0, headroom_db=0.0),
    )
    assert result["headend"]["active_carrier_count_full"] == 8
    assert result["repeaters"]["G41"]["full"] is not None
    assert result["repeaters"]["G43"]["full"] is not None


def test_power_label():
    assert power_label(1) == "power"
    assert power_label(2) == "composite power"
    assert power_label(0) == "power"


def test_four_g41_separate_ports():
    """4× G41 repeaters each into a dedicated service module port."""
    from throughput_calculator.power_budget_model import (
        TOPOLOGY_FOUR_G41_SEPARATE,
        default_carriers_four_g41,
        default_ports_four_g41_separate,
    )

    carriers = default_carriers_four_g41()
    ports = default_ports_four_g41_separate()
    headend = HeadendConfig(
        passive_loss_db=8.0,
        headroom_db=0.0,
        topology=TOPOLOGY_FOUR_G41_SEPARATE,
    )
    result = compute_power_budget(carriers, ports, headend)
    assert result["multi_port"] is True
    assert result["topology"] == TOPOLOGY_FOUR_G41_SEPARATE
    assert len(result["ports"]) == 4
    for port in result["ports"]:
        assert port["active_carrier_count_full"] == 2
        vm = port["verification_matrix"]["full_load"]
        # 2× +12 dBm G41 radios → +15 dBm composite; −4 dB jumper+splitter → +11 dBm hub
        assert abs(vm["repeater_out_dbm"] - 15.0) < 0.5
        assert abs(vm["hub_input_dbm"] - 11.0) < 0.5
        assert abs(vm["ru_output_dbm"] - 20.0) < 0.5
        assert port["calculated_dl_input_level"] == 13
        assert port["recommended_system_gain_db"] == 9.0
        assert abs(port["dl_path_loss_db"] - 4.0) < 0.1
        assert abs(port["ul_path_loss_db"] - 4.0) < 0.1
        assert port["overall_pass"] is True
        assert port["power_label_full"] == "composite power"
    assert result["all_ports_pass"] is True


def test_cel_fi_quatra_combined():
    from throughput_calculator.power_budget_model import (
        TOPOLOGY_CEL_FI_QUATRA_COMBINED,
        default_carriers_cel_fi_quatra,
        default_ports_cel_fi_quatra_combined,
        recommended_dl_agc_db,
    )

    carriers = default_carriers_cel_fi_quatra()
    ports = default_ports_cel_fi_quatra_combined()
    headend = HeadendConfig(topology=TOPOLOGY_CEL_FI_QUATRA_COMBINED, headroom_db=0.0)
    result = compute_power_budget(carriers, ports, headend)
    port = result["ports"][0]
    das = port["das_dl_service_port"]
    assert len(carriers) == 8
    assert result["multi_port"] is False
    assert port["active_carrier_count_full"] == 8
    assert "QUATRA" in result["repeaters"]
    # 8× +16 dBm → ~+25 dBm repeater; −4 dB jumper+splitter → ~+21 dBm raw hub (> +15 ceiling)
    assert das["attenuation_required"] is True
    assert abs(das["raw_dl_input_full_dbm"] - 21.0) < 0.5
    assert abs(port["dl_agc_db"] - 6.0) < 0.5
    assert abs(das["protected_dl_input_full_dbm"] - 15.0) < 0.5
    assert port["calculated_dl_input_level"] == 13
    assert abs(port["em_dl_gain_budget_db"] - 10.0) < 0.5
    assert abs(port["effective_max_dl_gain_db"] - 23.0) < 0.5
    assert abs(port["dl_path_loss_db"] - 10.0) < 0.5
    assert abs(port["ul_path_loss_db"] - 10.0) < 0.5
    assert das["within_hub_window"] is True
    assert port["overall_pass"] is True
    assert abs(port["recommended_system_gain_db"] - 5.0) < 0.5
    ul = port["das_ul_service_port"]
    assert abs(ul["duplex_splitter_ul_db"] - 3.0) < 0.1
    assert abs(ul["ul_attenuator_db"] - 6.0) < 0.1


def test_duplex_splitter_in_passive_chain():
    from throughput_calculator.power_budget_model import (
        passive_chain_breakdown,
        normalize_passive_loss_items,
    )

    items = normalize_passive_loss_items(None)
    chain = passive_chain_breakdown(items)
    assert chain["duplex_splitter_db"] == 3.0
    assert chain["duplex_splitter_dl_db"] == chain["duplex_splitter_ul_db"] == 3.0
    assert abs(chain["total_passive_excl_atten_db"] - 4.0) < 0.01


def test_duplex_splitter_shared_dl_ul():
    from throughput_calculator.power_budget_model import (
        passive_chain_breakdown,
        normalize_passive_loss_items,
    )

    items = normalize_passive_loss_items([
        {"name": "A", "loss_db": 1},
        {"name": "B", "loss_db": 2},
        {"name": "C", "loss_db": 0.5},
        {"name": "Duplex RF splitter (DL & UL branch)", "loss_db": 4.0},
        {"name": "Patch", "loss_db": 0.5},
    ])
    chain = passive_chain_breakdown(items)
    assert chain["duplex_splitter_ul_db"] == chain["duplex_splitter_dl_db"] == 4.0


def test_portal_ul_balance_unity():
    from throughput_calculator.power_budget_model import (
        net_ul_system_gain_db,
        recommended_portal_ul_balance_db,
        ul_hub_gain_from_portal_balance,
    )

    # zinwave.txt: −8 dB path, −17 dB balance → +8 dB hub UL gain → 0 dB net
    assert recommended_portal_ul_balance_db(8.0) == -17.0
    assert ul_hub_gain_from_portal_balance(0.0) == 25.0
    assert ul_hub_gain_from_portal_balance(-15.0) == 10.0
    assert abs(net_ul_system_gain_db(8.0, 8.0)) < 0.01
    assert abs(net_ul_system_gain_db(25.0, 8.0) - 17.0) < 0.01


def test_portal_ul_balance_on_port():
    from throughput_calculator.power_budget_model import (
        default_carriers_cel_fi_quatra,
        default_ports_cel_fi_quatra_combined,
    )

    ports = default_ports_cel_fi_quatra_combined()
    headend = HeadendConfig(headroom_db=0.0)
    result = compute_power_budget(default_carriers_cel_fi_quatra(), ports, headend)
    ul = result["ports"][0]["das_ul_service_port"]
    assert ul["portal_ul_balance_db"] == -15.0
    assert ul["ul_hub_gain_db"] is not None
    assert ul["net_ul_system_gain_db"] is not None


def test_dl_attenuator_after_splitter():
    from throughput_calculator.power_budget_model import (
        TOPOLOGY_CEL_FI_QUATRA_COMBINED,
        default_carriers_cel_fi_quatra,
        default_ports_cel_fi_quatra_combined,
    )

    carriers = default_carriers_cel_fi_quatra()
    ports = default_ports_cel_fi_quatra_combined()
    ports[0].dl_agc_db = 2.0
    ports[0].ul_attenuation_db = 2.0
    headend = HeadendConfig(topology=TOPOLOGY_CEL_FI_QUATRA_COMBINED, headroom_db=0.0)
    result = compute_power_budget(carriers, ports, headend)
    port = result["ports"][0]
    das = port["das_dl_service_port"]
    assert abs(das["raw_dl_input_full_dbm"] - 21.0) < 0.5
    assert abs(das["protected_dl_input_full_dbm"] - 19.0) < 0.5
    assert das["attenuation_still_required"] is True
    assert abs(das["remaining_external_attenuation_db"] - 4.0) < 0.5
    assert port["ul_attenuation_db"] == 2.0
