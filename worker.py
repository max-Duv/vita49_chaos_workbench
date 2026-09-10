#!/usr/bin/env python3

import collections
import json
import os
import threading
import time

import numpy as np

from faults import ChaosEngine
from metrics import StreamMetrics


class ExperimentLogger:
    def __init__(self, directory="logs"):
        self.directory = directory
        self.fp = None
        self.path = None
        self.lock = threading.Lock()

    def start(self, meta):
        os.makedirs(self.directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(self.directory, "vita49_chaos_%s.jsonl" % stamp)
        self.fp = open(self.path, "w", buffering=1)
        self.write("experiment_start", meta)

    def write(self, kind, payload):
        if not self.fp:
            return
        record = {"time": time.time(), "kind": kind, "data": payload}
        with self.lock:
            self.fp.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def close(self, summary=None):
        if self.fp:
            if summary is not None:
                self.write("experiment_end", summary)
            self.fp.close()
            self.fp = None


class WorkbenchWorker(threading.Thread):
    def __init__(self, source, emitter, fs, dtype_name, iq, seed=1, log_dir="logs"):
        super().__init__(daemon=True)
        self.source = source
        self.emitter = emitter
        self.fs = float(fs)
        self.dtype_name = dtype_name
        self.iq = bool(iq)
        self.engine = ChaosEngine(fs, dtype_name, iq, seed)
        self.input_metrics = StreamMetrics(fs, dtype_name, iq)
        self.output_metrics = StreamMetrics(fs, dtype_name, iq)
        self.input_chunks = collections.deque(maxlen=512)
        self.output_chunks = collections.deque(maxlen=512)
        self.events = collections.deque(maxlen=4096)
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.error = None
        self.finished = False
        self.emit_enabled = False
        self.logger = ExperimentLogger(log_dir)
        self.started_wall = None
        self._last_metrics_log = 0.0
        self._packet_index = 0

    def begin_experiment(self, cfg, seed, emit_enabled=False, meta=None):
        self.engine.reseed(seed)
        self.engine.reset()
        self.engine.config.set(cfg)
        self.engine.set_active(True)
        self.input_metrics.reset()
        self.output_metrics.reset()
        self.emit_enabled = bool(emit_enabled)
        self.started_wall = time.time()
        if self.logger.fp:
            self.logger.close()
        info = {"seed": int(seed), "config": cfg, "emit_enabled": self.emit_enabled}
        if meta:
            info.update(meta)
        self.logger.start(info)
        with self.lock:
            self.events.append((time.time(), "START", "experiment"))

    def stop_experiment(self):
        self.engine.set_active(False)
        self.emit_enabled = False
        summary = self.snapshot()
        if self.logger.fp:
            self.logger.close(summary)
        with self.lock:
            self.events.append((time.time(), "STOP", "experiment"))

    def run(self):
        try:
            self.source.open()
            while not self.stop_evt.is_set():
                ev = self.source.read(0.02)
                if ev is not None:
                    self._packet_index += 1
                    x = self.input_metrics.update(ev.raw)
                    if len(x):
                        with self.lock:
                            self.input_chunks.append(x)
                    outs = self.engine.ingest(ev.raw, time.monotonic())
                    self._handle_outputs(outs)
                    self._apply_system_faults()
                else:
                    self._handle_outputs(self.engine.poll(time.monotonic()))
                    if getattr(self.source, "finished", False):
                        break
                self._log_periodic()
            self._handle_outputs(self.engine.flush())
        except Exception as e:
            self.error = str(e)
        finally:
            self.finished = True
            try: self.source.close()
            except Exception: pass
            try:
                if self.emitter: self.emitter.close()
            except Exception: pass
            if self.logger.fp:
                self.logger.close(self.snapshot())

    def _apply_system_faults(self):
        if not self.engine.active:
            return
        cfg = self.engine.config.get()["system"]
        d = float(cfg["processing_delay_ms"])
        if d > 0:
            time.sleep(d / 1000.0)
        every = int(cfg["consumer_stall_every"])
        stall = float(cfg["consumer_stall_ms"])
        if every > 0 and stall > 0 and self._packet_index % every == 0:
            self._event("consumer_stall", "%g ms" % stall)
            time.sleep(stall / 1000.0)

    def _handle_outputs(self, outs):
        for p in outs:
            y = self.output_metrics.update(p.raw)
            if len(y):
                with self.lock:
                    self.output_chunks.append(y)
            if self.emit_enabled and self.emitter:
                self.emitter.send(p.raw)
            if p.faults:
                for f in p.faults:
                    self._event(f, "packet")

    def _event(self, name, detail=""):
        ts = time.time()
        with self.lock:
            self.events.append((ts, name, detail))
        self.logger.write("fault", {"name": name, "detail": detail})

    def _log_periodic(self):
        now = time.time()
        if now - self._last_metrics_log >= 1.0:
            self._last_metrics_log = now
            if self.logger.fp:
                self.logger.write("metrics", {
                    "input": self.input_metrics.snapshot(),
                    "output": self.output_metrics.snapshot(),
                    "engine": dict(self.engine.stats),
                })

    def drain_samples(self):
        with self.lock:
            if self.input_chunks:
                xi = np.concatenate(tuple(self.input_chunks))
                self.input_chunks.clear()
            else:
                xi = np.empty(0, dtype=np.complex128 if self.iq else np.float64)
            if self.output_chunks:
                xo = np.concatenate(tuple(self.output_chunks))
                self.output_chunks.clear()
            else:
                xo = np.empty(0, dtype=np.complex128 if self.iq else np.float64)
            return xi, xo

    def get_events(self):
        with self.lock:
            return list(self.events)

    def snapshot(self):
        try:
            source_status = self.source.status() if hasattr(self.source, "status") else {}
        except Exception as e:
            source_status = {"backend": "unknown", "status_error": str(e)}
        return {
            "input": self.input_metrics.snapshot(),
            "output": self.output_metrics.snapshot(),
            "engine": dict(self.engine.stats),
            "source": source_status,
            "active": self.engine.active,
            "error": self.error,
            "finished": self.finished,
            "log_path": self.logger.path,
        }

    def stop(self):
        self.stop_evt.set()
