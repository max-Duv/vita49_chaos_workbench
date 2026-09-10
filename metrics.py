#!/usr/bin/env python3

import collections
import threading
import time

import numpy as np

from vita49 import VRTFrame, decode_payload


class StreamMetrics:
    def __init__(self, fs, dtype_name, iq):
        self.fs = float(fs)
        self.dtype_name = dtype_name
        self.iq = bool(iq)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", _NullLock()):
            self.t0 = time.time()
            self.packets = 0
            self.bytes = 0
            self.samples = 0
            self.seq_gaps = 0
            self.reorders = 0
            self.ts_drops = 0
            self.glitches = 0
            self.parse_errors = 0
            self.sid = None
            self.last_seq = None
            self.last_ts = None
            self.ts_deltas = collections.deque(maxlen=256)

    def update(self, raw):
        try:
            f = VRTFrame.parse(raw)
        except Exception:
            with self.lock:
                self.parse_errors += 1
                self.packets += 1
                self.bytes += len(raw)
            return np.empty(0, dtype=np.complex128 if self.iq else np.float64)
        try:
            x = decode_payload(f, self.dtype_name, self.iq)
        except Exception:
            x = np.empty(0, dtype=np.complex128 if self.iq else np.float64)
        ts = f.timestamp_seconds()
        with self.lock:
            self.packets += 1
            self.bytes += len(raw)
            self.samples += len(x)
            if f.stream_id is not None:
                self.sid = f.stream_id
            if self.last_seq is not None:
                expected = (self.last_seq + 1) & 0xF
                if f.packet_count != expected:
                    forward = (f.packet_count - expected) & 0xF
                    backward = (expected - f.packet_count) & 0xF
                    if forward <= backward:
                        self.seq_gaps += max(1, forward)
                    else:
                        self.reorders += 1
            self.last_seq = f.packet_count
            if ts is not None and self.last_ts is not None:
                dt = ts - self.last_ts
                if dt <= 0:
                    self.glitches += 1
                else:
                    self.ts_deltas.append(dt)
                    expected_dt = (len(x) / self.fs) if len(x) else 0.0
                    if expected_dt > 0 and dt > expected_dt * 1.5:
                        self.ts_drops += 1
                    if expected_dt > 0 and abs(dt - expected_dt) > max(expected_dt * 0.25, 1e-4):
                        self.glitches += 1
            if ts is not None:
                self.last_ts = ts
        return x

    def snapshot(self):
        with self.lock:
            elapsed = max(time.time() - self.t0, 1e-6)
            est_fs = None
            if self.ts_deltas:
                med = float(np.median(np.asarray(self.ts_deltas)))
                # Packet sample count is not persisted per delta, so use observed
                # sample rate for the GUI and expose timestamp health separately.
                if med > 0:
                    est_fs = self.samples / elapsed
            return {
                "packets": self.packets,
                "pps": self.packets / elapsed,
                "obs": self.samples / elapsed,
                "mbps": (self.bytes * 8.0 / 1e6) / elapsed,
                "est_fs": est_fs,
                "seq_gaps": self.seq_gaps,
                "reorders": self.reorders,
                "ts_drops": self.ts_drops,
                "glitches": self.glitches,
                "parse_errors": self.parse_errors,
                "sid": "0x%08X" % self.sid if self.sid is not None else "?",
            }


class _NullLock:
    def __enter__(self): return self
    def __exit__(self, *args): return False
