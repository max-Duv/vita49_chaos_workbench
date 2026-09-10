#!/usr/bin/env python3

import copy
from faults import default_config


def _base():
    return default_config()


def packet_path_degradation():
    c = _base()
    c["transport"].update(drop_pct=5.0, duplicate_pct=1.0, jitter_ms=8.0,
                          reorder_pct=8.0, reorder_window=5)
    return c


def burst_outage():
    c = _base()
    c["transport"].update(burst_enabled=True, burst_drop_packets=18,
                          burst_every_packets=250, jitter_ms=2.0)
    return c


def timing_collapse():
    c = _base()
    c["protocol"].update(ts_drift_ppm=250.0, ts_jitter_us=150.0,
                         seq_mode="jump", seq_jump=3, seq_every_packets=64)
    return c


def protocol_corruption():
    c = _base()
    c["protocol"].update(seq_mode="random", seq_every_packets=24,
                         truncate_pct=1.0, truncate_bytes=8,
                         header_bitflip_pct=0.5)
    return c


def rf_degradation():
    c = _base()
    c["signal"].update(enabled=True, gain=0.65, noise_snr_db=14.0,
                       sample_dropout_pct=0.5, tone_enabled=True,
                       tone_hz=3200.0, tone_amp=500.0)
    return c


def iq_path_fault():
    c = _base()
    c["signal"].update(enabled=True, iq_swap=True, iq_conjugate=True,
                       phase_deg=30.0)
    return c


def consumer_stall():
    c = _base()
    c["system"].update(consumer_stall_every=200, consumer_stall_ms=300.0)
    return c


def mixed_failure():
    c = packet_path_degradation()
    c["protocol"].update(ts_drift_ppm=80.0, ts_jitter_us=50.0,
                         seq_mode="jump", seq_jump=2, seq_every_packets=100)
    c["signal"].update(enabled=True, gain=0.8, noise_snr_db=18.0,
                       sample_dropout_pct=0.2)
    return c


PRESETS = {
    "Clean / pass-through": _base,
    "Packet path degradation": packet_path_degradation,
    "Burst outage": burst_outage,
    "Timing collapse": timing_collapse,
    "Protocol corruption": protocol_corruption,
    "RF degradation": rf_degradation,
    "I/Q path fault": iq_path_fault,
    "Consumer stall invariant": consumer_stall,
    "Mixed failure": mixed_failure,
}
