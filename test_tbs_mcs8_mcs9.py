"""
Verify MCS 8 and MCS 9 both get valid TBS throughput at 20 MHz (no 0.01 fallback).
Run from repo root: python -m pytest throughput_calculator/test_tbs_mcs8_mcs9.py -v
Or: python throughput_calculator/test_tbs_mcs8_mcs9.py
"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "src"))


def test_mcs_8_and_9_throughput_at_20mhz():
    from throughput_calculator.throughput_model import throughput_from_mcs_mbps
    tp8 = throughput_from_mcs_mbps(8, 20.0, 0.15, rsrp_dbm=None)
    tp9 = throughput_from_mcs_mbps(9, 20.0, 0.15, rsrp_dbm=None)
    assert tp8 > 1.0, f"MCS 8 @ 20 MHz should be > 1 Mbit/s (TBS), got {tp8}"
    assert tp9 > 1.0, f"MCS 9 @ 20 MHz should be > 1 Mbit/s (TBS), got {tp9}"
    assert 5.0 < tp8 < 25.0, f"MCS 8 @ 20 MHz expected ~8-10 Mbit/s, got {tp8}"
    assert 10.0 < tp9 < 25.0, f"MCS 9 @ 20 MHz expected ~15-20 Mbit/s, got {tp9}"


def test_mcs_8_10mhz_siso_gives_about_6_mbps():
    """1 LTE carrier, 10 MHz, MCS 8, no congestion, SISO -> expect ~6 Mbit/s."""
    from throughput_calculator.throughput_model import throughput_from_mcs_mbps
    tp = throughput_from_mcs_mbps(8, 10.0, 0.0, rsrp_dbm=None)  # SISO = rank2 0%
    assert 5.0 <= tp <= 7.0, f"MCS 8 @ 10 MHz SISO expected ~6 Mbit/s, got {tp}"


if __name__ == "__main__":
    # Run without pytest
    from throughput_calculator.throughput_model import throughput_from_mcs_mbps
    tp8_20 = throughput_from_mcs_mbps(8, 20.0, 0.15, rsrp_dbm=None)
    tp9_20 = throughput_from_mcs_mbps(9, 20.0, 0.15, rsrp_dbm=None)
    tp8_10_siso = throughput_from_mcs_mbps(8, 10.0, 0.0, rsrp_dbm=None)
    print(f"MCS 8 @ 20 MHz: {tp8_20:.4f} Mbit/s")
    print(f"MCS 9 @ 20 MHz: {tp9_20:.4f} Mbit/s")
    print(f"MCS 8 @ 10 MHz SISO: {tp8_10_siso:.4f} Mbit/s (expect ~6)")
    if tp8_20 > 1 and tp9_20 > 1 and 5 <= tp8_10_siso <= 7:
        print("OK: TBS and 10 MHz mapping correct")
    else:
        print("FAIL")
        sys.exit(1)
