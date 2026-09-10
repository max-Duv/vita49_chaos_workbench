#!/usr/bin/env python3

import copy
import heapq
import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from vita49 import VRTFrame, decode_payload, encode_payload


def default_config():
    return {
        "transport": {
            "drop_pct": 0.0,
            "burst_enabled": False,
            "burst_drop_packets": 0,
            "burst_every_packets": 0,
            "blackout": False,
            "duplicate_pct": 0.0,
            "fixed_delay_ms": 0.0,
            "jitter_ms": 0.0,
            "reorder_pct": 0.0,
            "reorder_window": 4,
            "throttle_pps": 0.0,
        },
        "protocol": {
            "seq_mode": "off",          # off/jump/freeze/random
            "seq_jump": 1,
            "seq_every_packets": 0,
            "sid_enabled": False,
            "sid_value": 0xDEADBEEF,
            "ts_offset_ms": 0.0,
            "ts_drift_ppm": 0.0,
            "ts_jitter_us": 0.0,
            "ts_freeze": False,
            "ts_step_ms": 0.0,
            "ts_step_every_packets": 0,
            "truncate_pct": 0.0,
            "truncate_bytes": 4,
            "truncate_keep_size": True,
            "header_bitflip_pct": 0.0,
            "payload_bitflip_pct": 0.0,
            "payload_bitflip_bits": 1,
        },
        "signal": {
            "enabled": False,
            "gain": 1.0,
            "dc_offset": 0.0,
            "noise_snr_db": 0.0,         # <=0 disables AWGN
            "clip_abs": 0.0,             # <=0 disables
            "zero_pct": 0.0,
            "sample_dropout_pct": 0.0,
            "stuck_enabled": False,
            "stuck_value": 0.0,
            "tone_enabled": False,
            "tone_hz": 1000.0,
            "tone_amp": 0.0,
            "iq_swap": False,
            "iq_conjugate": False,
            "phase_deg": 0.0,
        },
        "system": {
            "processing_delay_ms": 0.0,
            "consumer_stall_every": 0,
            "consumer_stall_ms": 0.0,
        },
    }


class ConfigStore:
    def __init__(self):
        self._cfg = default_config()
        self._lock = threading.Lock()

    def set(self, cfg):
        with self._lock:
            self._cfg = copy.deepcopy(cfg)

    def get(self):
        with self._lock:
            return copy.deepcopy(self._cfg)


@dataclass(order=True)
class ScheduledPacket:
    due: float
    order: int
    raw: bytes = field(compare=False)
    faults: Tuple[str, ...] = field(default_factory=tuple, compare=False)


class ChaosEngine:
    def __init__(self, fs: float, dtype_name: str, iq: bool, seed: int = 1):
        self.fs = float(fs)
        self.dtype_name = dtype_name
        self.iq = bool(iq)
        self.seed = int(seed)
        self.rng = random.Random(self.seed)
        self.nprng = np.random.RandomState(self.seed & 0xFFFFFFFF)
        self.active = False
        self.config = ConfigStore()
        self.heap: List[ScheduledPacket] = []
        self.reorder_buf: List[Tuple[bytes, Tuple[str, ...]]] = []
        self.counter = 0
        self.last_emit_due = 0.0
        self.input_index = 0
        self.frozen_seq = None
        self.frozen_ts = None
        self.ts_origin_input = None
        self.ts_origin_wall = None
        self.tone_phase = 0.0
        self.stats = {
            "seen": 0, "scheduled": 0, "emitted": 0, "dropped": 0,
            "duplicates": 0, "mutated": 0, "reordered": 0,
        }
        self.last_events = []

    def reseed(self, seed: int):
        self.seed = int(seed)
        self.rng.seed(self.seed)
        self.nprng.seed(self.seed & 0xFFFFFFFF)

    def reset(self):
        self.heap[:] = []
        self.reorder_buf[:] = []
        self.counter = 0
        self.last_emit_due = 0.0
        self.input_index = 0
        self.frozen_seq = None
        self.frozen_ts = None
        self.ts_origin_input = None
        self.ts_origin_wall = None
        self.tone_phase = 0.0
        for k in self.stats:
            self.stats[k] = 0
        self.last_events = []

    def set_active(self, active: bool):
        self.active = bool(active)
        if not active:
            self.frozen_seq = None
            self.frozen_ts = None

    @staticmethod
    def _chance(rng, pct):
        return pct > 0.0 and rng.random() < (float(pct) / 100.0)

    def ingest(self, raw: bytes, now: Optional[float] = None):
        if now is None:
            now = time.monotonic()
        self.stats["seen"] += 1
        self.input_index += 1
        cfg = self.config.get()
        faults = []

        if not self.active:
            self._schedule(raw, now, faults, cfg)
            return self.poll(now)

        tcfg = cfg["transport"]
        if tcfg["blackout"]:
            self.stats["dropped"] += 1
            self.last_events.append((time.time(), "blackout_drop"))
            return self.poll(now)

        burst = (tcfg["burst_enabled"] and tcfg["burst_every_packets"] > 0 and
                 ((self.input_index - 1) % int(tcfg["burst_every_packets"])) < int(tcfg["burst_drop_packets"]))
        if burst or self._chance(self.rng, tcfg["drop_pct"]):
            self.stats["dropped"] += 1
            self.last_events.append((time.time(), "burst_drop" if burst else "random_drop"))
            return self.poll(now)

        mutated, mfaults = self._mutate_packet(raw, cfg)
        faults.extend(mfaults)
        copies = 2 if self._chance(self.rng, tcfg["duplicate_pct"]) else 1
        if copies == 2:
            self.stats["duplicates"] += 1
            faults.append("duplicate")

        for ci in range(copies):
            cfaults = tuple(faults + (["duplicate_copy"] if ci else []))
            if self._chance(self.rng, tcfg["reorder_pct"]):
                self.reorder_buf.append((mutated, cfaults + ("reorder",)))
                self.stats["reordered"] += 1
                if len(self.reorder_buf) >= max(2, int(tcfg["reorder_window"])):
                    idx = self.rng.randrange(len(self.reorder_buf))
                    rraw, rflags = self.reorder_buf.pop(idx)
                    self._schedule(rraw, now, rflags, cfg)
            else:
                self._schedule(mutated, now, cfaults, cfg)

        # Periodically release one buffered reordered packet to prevent starvation.
        if self.reorder_buf and (self.input_index % max(2, int(tcfg["reorder_window"]))) == 0:
            idx = self.rng.randrange(len(self.reorder_buf))
            rraw, rflags = self.reorder_buf.pop(idx)
            self._schedule(rraw, now, rflags, cfg)
        return self.poll(now)

    def flush(self):
        now = time.monotonic()
        cfg = self.config.get()
        for raw, flags in self.reorder_buf:
            self._schedule(raw, now, flags, cfg)
        self.reorder_buf[:] = []
        out = []
        while self.heap:
            p = heapq.heappop(self.heap)
            out.append(p)
        return out

    def _schedule(self, raw, now, faults, cfg):
        tcfg = cfg["transport"]
        delay = max(0.0, float(tcfg["fixed_delay_ms"]) / 1000.0)
        if tcfg["jitter_ms"] > 0:
            delay += max(0.0, self.rng.gauss(0.0, float(tcfg["jitter_ms"]) / 1000.0))
            faults = tuple(faults) + ("jitter",)
        due = now + delay
        pps = float(tcfg["throttle_pps"])
        if pps > 0:
            due = max(due, self.last_emit_due + 1.0 / pps)
            self.last_emit_due = due
            faults = tuple(faults) + ("throttle",)
        self.counter += 1
        heapq.heappush(self.heap, ScheduledPacket(due, self.counter, raw, tuple(faults)))
        self.stats["scheduled"] += 1

    def poll(self, now: Optional[float] = None):
        if now is None:
            now = time.monotonic()
        out = []
        while self.heap and self.heap[0].due <= now:
            p = heapq.heappop(self.heap)
            out.append(p)
            self.stats["emitted"] += 1
        return out

    def _mutate_packet(self, raw: bytes, cfg):
        faults = []
        try:
            frame = VRTFrame.parse(raw)
        except Exception:
            return raw, faults
        pcfg = cfg["protocol"]

        mode = pcfg["seq_mode"]
        every = int(pcfg["seq_every_packets"])
        apply_seq = mode != "off" and (every <= 1 or self.input_index % every == 0)
        if apply_seq:
            if mode == "jump":
                frame.set_packet_count(frame.packet_count + int(pcfg["seq_jump"]))
            elif mode == "freeze":
                if self.frozen_seq is None:
                    self.frozen_seq = frame.packet_count
                frame.set_packet_count(self.frozen_seq)
            elif mode == "random":
                frame.set_packet_count(self.rng.randrange(16))
            faults.append("sequence_%s" % mode)

        if pcfg["sid_enabled"] and frame.set_stream_id(int(pcfg["sid_value"])):
            faults.append("stream_id")

        ts = frame.timestamp_seconds()
        if ts is not None:
            if pcfg["ts_freeze"]:
                if self.frozen_ts is None:
                    self.frozen_ts = ts
                ts_new = self.frozen_ts
                frame.set_timestamp_seconds(ts_new)
                faults.append("timestamp_freeze")
            else:
                ts_new = ts + float(pcfg["ts_offset_ms"]) / 1000.0
                ppm = float(pcfg["ts_drift_ppm"])
                if ppm:
                    if self.ts_origin_input is None:
                        self.ts_origin_input = ts
                    ts_new += (ts - self.ts_origin_input) * ppm * 1e-6
                    faults.append("timestamp_drift")
                if pcfg["ts_jitter_us"] > 0:
                    ts_new += self.rng.gauss(0.0, float(pcfg["ts_jitter_us"]) * 1e-6)
                    faults.append("timestamp_jitter")
                step_every = int(pcfg.get("ts_step_every_packets", 0))
                if step_every > 0 and self.input_index % step_every == 0 and float(pcfg.get("ts_step_ms", 0.0)) != 0.0:
                    ts_new += float(pcfg["ts_step_ms"]) / 1000.0
                    faults.append("timestamp_step")
                if abs(ts_new - ts) > 0:
                    frame.set_timestamp_seconds(ts_new)
                    if pcfg["ts_offset_ms"]:
                        faults.append("timestamp_offset")

        if self._chance(self.rng, pcfg["header_bitflip_pct"]) and len(frame.raw) >= 4:
            # Keep bit flips in the first word, but avoid destroying packet-size
            # geometry by excluding the low 16 bits.
            bit = self.rng.randrange(16, 32)
            byte_idx = 3 - (bit // 8)
            bit_idx = bit % 8
            frame.raw[byte_idx] ^= (1 << bit_idx)
            faults.append("header_bitflip")

        if self._chance(self.rng, pcfg["truncate_pct"]):
            frame.truncate(int(pcfg["truncate_bytes"]), bool(pcfg["truncate_keep_size"]))
            faults.append("truncate")

        scfg = cfg["signal"]
        if scfg["enabled"]:
            try:
                x = decode_payload(frame, self.dtype_name, self.iq)
                if len(x):
                    x, sfaults = self._signal_mutate(x, scfg)
                    faults.extend(sfaults)
                    if sfaults:
                        frame.replace_payload(encode_payload(x, self.dtype_name, self.iq))
            except Exception:
                pass

        if self._chance(self.rng, pcfg.get("payload_bitflip_pct", 0.0)) and frame.payload_end > frame.payload_offset:
            nbits = max(1, int(pcfg.get("payload_bitflip_bits", 1)))
            for _ in range(nbits):
                bi = self.rng.randrange(frame.payload_offset, frame.payload_end)
                frame.raw[bi] ^= (1 << self.rng.randrange(8))
            faults.append("payload_bitflip")

        if faults:
            self.stats["mutated"] += 1
            for f in faults:
                self.last_events.append((time.time(), f))
            if len(self.last_events) > 5000:
                self.last_events = self.last_events[-2500:]
        return frame.bytes(), faults

    def _signal_mutate(self, x, scfg):
        y = np.asarray(x).copy()
        faults = []
        gain = float(scfg["gain"])
        if abs(gain - 1.0) > 1e-12:
            y *= gain
            faults.append("gain")
        dc = float(scfg["dc_offset"])
        if dc:
            y += dc
            faults.append("dc_offset")
        if scfg["zero_pct"] > 0 and self._chance(self.rng, scfg["zero_pct"]):
            y[:] = 0
            faults.append("zero_payload")
        drop_pct = float(scfg["sample_dropout_pct"])
        if drop_pct > 0:
            mask = self.nprng.rand(len(y)) < drop_pct / 100.0
            if mask.any():
                y[mask] = 0
                faults.append("sample_dropout")
        if scfg.get("stuck_enabled", False):
            y[:] = float(scfg.get("stuck_value", 0.0))
            faults.append("stuck_sample")
        snr = float(scfg["noise_snr_db"])
        if snr > 0:
            sigp = float(np.mean(np.abs(y) ** 2)) + 1e-18
            npow = sigp / (10.0 ** (snr / 10.0))
            if np.iscomplexobj(y):
                noise = (self.nprng.normal(size=len(y)) + 1j * self.nprng.normal(size=len(y)))
                noise *= math.sqrt(npow / 2.0)
            else:
                noise = self.nprng.normal(size=len(y)) * math.sqrt(npow)
            y = y + noise
            faults.append("awgn")
        if scfg["tone_enabled"] and float(scfg["tone_amp"]) != 0:
            n = np.arange(len(y))
            phase = self.tone_phase + 2 * np.pi * float(scfg["tone_hz"]) * n / self.fs
            if np.iscomplexobj(y):
                tone = float(scfg["tone_amp"]) * np.exp(1j * phase)
            else:
                tone = float(scfg["tone_amp"]) * np.sin(phase)
            y = y + tone
            self.tone_phase = float((phase[-1] + 2 * np.pi * float(scfg["tone_hz"]) / self.fs) % (2*np.pi))
            faults.append("tone")
        clip_abs = float(scfg["clip_abs"])
        if clip_abs > 0:
            if np.iscomplexobj(y):
                y = np.clip(y.real, -clip_abs, clip_abs) + 1j*np.clip(y.imag, -clip_abs, clip_abs)
            else:
                y = np.clip(y, -clip_abs, clip_abs)
            faults.append("clipping")
        if self.iq:
            if scfg["iq_swap"]:
                y = y.imag + 1j*y.real
                faults.append("iq_swap")
            if scfg["iq_conjugate"]:
                y = np.conjugate(y)
                faults.append("iq_conjugate")
            phase_deg = float(scfg["phase_deg"])
            if phase_deg:
                y = y * np.exp(1j * np.deg2rad(phase_deg))
                faults.append("phase_rotation")
        return y, faults
