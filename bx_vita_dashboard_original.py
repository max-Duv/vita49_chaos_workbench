#!/usr/bin/env python3
"""
RS-34 Bx VITA-49 real-time dashboard (pyqtgraph) -- passive receive + display.

Same posture as bx_vita_scope.py: this program does NOT transmit, craft, or
inject anything. It runs `sudo -n tshark` as a child (run `sudo -v` once first),
reassembles + dissects VITA-49/VRT, and displays a scrolling waterfall, live
spectrum, waveform, and a link-health panel.

Why pyqtgraph instead of matplotlib: the waterfall scrolls via a single
GraphicsScene update instead of a full canvas redraw, so it keeps up with the
~500 pkt/s (250 kSa/s) feed; the histogram gives interactive level/colormap
control; capture runs on a background thread so GUI hitches never cause real
packet loss.

Requires (Python 3.6 friendly):
    pip3 install --user "pyqtgraph==0.11.1" "PyQt5==5.15.2"
        (or use the distro package: dnf install python3-qt5)
    numpy is already a dependency of bx_vita_scope.py

Run (NOT as root):
    sudo -v
    python3 bx_vita_dashboard.py live-tshark
    python3 bx_vita_dashboard.py pcap ~/rs34_vita_baseline.pcap
"""

import argparse
import atexit
import collections
import shutil
import signal
import subprocess
import sys
import threading
import time

import numpy as np

import bx_vita_scope as core

try:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui
    try:
        from pyqtgraph.Qt import QtWidgets
    except Exception:                       # very old pyqtgraph
        QtWidgets = QtGui
    from pyqtgraph.dockarea import DockArea, Dock
except Exception as e:                       # pragma: no cover - env dependent
    sys.stderr.write(
        "pyqtgraph/PyQt5 not available: %s\n"
        "Install with: pip3 install --user 'pyqtgraph==0.11.1' 'PyQt5==5.15.2'\n"
        % e)
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Background capture thread. Owns tshark, decode, and the trackers so the GUI
# thread only ever touches numpy buffers and a stats snapshot.
# ---------------------------------------------------------------------------
class Reader(threading.Thread):
    def __init__(self, args):
        super(Reader, self).__init__()
        self.daemon = True
        self.args = args
        self._stop = threading.Event()
        self.proc = None
        self.lock = threading.Lock()

        cdtype = np.complex128 if args.iq else np.float64
        self._empty = np.empty(0, dtype=cdtype)
        # bounded hand-off buffer: a few seconds of chunks. maxlen bounds memory
        # if the GUI stalls (display-only loss, never mistaken for link loss).
        approx_pps = max(1, int(args.fs / max(core.SAMPLES_PER_PKT, 1)))
        self.chunks = collections.deque(maxlen=approx_pps * 4)

        self.seq = core.SeqTracker()
        self.timing = core.TimingTracker(core.SAMPLES_PER_PKT)
        self.packets = 0
        self.parse_errors = 0
        self.sample_loss = 0
        self.total_samples = 0
        self.sid = "?"
        self.error = None
        self.finished = False
        self.t0 = time.time()

    # -- command construction (reuses the engine's shared builders) --
    def _build_cmd(self):
        if self.args.mode == "pcap":
            cmd, _ = core.build_pcap_cmd(
                self.args.pcap, self.args.group, self.args.port, self.args.source)
            print("reading %s" % self.args.pcap)
            return cmd
        cmd, cap, disp = core.build_live_cmd(
            self.args.interface, self.args.group, self.args.port,
            self.args.source, self.args.src_mac, self.args.src_port)
        print("capture filter : %s" % cap)
        print("display filter : %s" % disp)
        print("")
        return cmd

    def run(self):
        try:
            if shutil.which("tshark") is None:
                raise RuntimeError("tshark not found")
            if self.args.mode != "pcap" and shutil.which("sudo") is None:
                raise RuntimeError("sudo not found")
            cmd = self._build_cmd()
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, bufsize=1)
            while not self._stop.is_set():
                line = self.proc.stdout.readline()
                if line == "":
                    if self.proc.poll() is not None:
                        err = self.proc.stderr.read()
                        if self.proc.returncode not in (0, None):
                            raise RuntimeError(
                                err.strip() or
                                "tshark exited. Run `sudo -v` first.")
                        break
                    continue
                self._handle(core._split_row(line))
        except Exception as e:
            with self.lock:
                self.error = str(e)
        finally:
            with self.lock:
                self.finished = True
            self._terminate()

    def _handle(self, row):
        data_hex = row[core.I_DATA]
        if not data_hex:
            return
        try:
            x = core.decode_vrt_data_hex(data_hex, self.args.dtype, self.args.iq)
        except Exception:
            with self.lock:
                self.parse_errors += 1
            return

        try:
            self.seq.update(int(row[core.I_SEQ], 0))
        except Exception:
            pass
        self.timing.update(row[core.I_TSI], row[core.I_TSF], self.args.fs)
        loss = row[core.I_LOSS].strip() == "1"

        with self.lock:
            self.packets += 1
            self.total_samples += len(x)
            if loss:
                self.sample_loss += 1
            if row[core.I_SID]:
                self.sid = row[core.I_SID]
            self.chunks.append(x)

    # -- GUI-thread accessors --
    def drain(self):
        with self.lock:
            if not self.chunks:
                return self._empty
            arr = np.concatenate(tuple(self.chunks))
            self.chunks.clear()
            return arr

    def snapshot(self):
        with self.lock:
            elapsed = max(time.time() - self.t0, 1e-6)
            return {
                "packets": self.packets,
                "pps": self.packets / elapsed,
                "obs": self.total_samples / elapsed,
                "est_fs": self.timing.est_fs(),
                "seq_gaps": self.seq.gaps,
                "ts_drops": self.timing.ts_drops,
                "reorders": self.seq.reorders,
                "glitches": self.timing.time_glitches,
                "sample_loss": self.sample_loss,
                "parse_errors": self.parse_errors,
                "sid": self.sid,
                "error": self.error,
                "finished": self.finished,
            }

    def reset_counters(self):
        with self.lock:
            self.seq = core.SeqTracker()
            self.timing = core.TimingTracker(core.SAMPLES_PER_PKT)
            self.sample_loss = 0
            self.parse_errors = 0
            self.packets = 0
            self.total_samples = 0
            self.t0 = time.time()

    def stop(self):
        self._stop.set()
        self._terminate()

    def _terminate(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Dashboard window.
# ---------------------------------------------------------------------------
GREEN = "#7CFC7C"
RED = "#FF6B6B"
DIM = "#B0B0B0"

# fields that should turn red when non-zero
ALARM_FIELDS = ("seq_gaps", "ts_drops", "reorders", "glitches",
                "sample_loss", "parse_errors")


class Dashboard(QtWidgets.QMainWindow):
    def __init__(self, reader, args):
        super(Dashboard, self).__init__()
        self.reader = reader
        self.args = args
        self.iq = args.iq
        self.fs = float(args.fs)
        self.nfft = args.nfft
        self.hop = args.hop
        self.win = np.hanning(self.nfft)
        self.paused = False

        self.setWindowTitle("RS-34 Bx VITA-49 Dashboard  (SID target 0x42783031)")
        self.resize(1280, 820)

        # spectral geometry
        self.wf_freqs = core.spectrogram_freqs_khz(self.nfft, self.fs, self.iq)
        self.nbins = core.spectrogram_bins(self.nfft, self.iq)
        self.wf_cols = max(256, int(args.wf_seconds * self.fs / self.hop))
        self.wf = np.full((self.nbins, self.wf_cols), -140.0, np.float32)

        # dedicated (finer) transform for the instantaneous spectrum panel
        self.spec_nfft = min(16384, 1 << int(np.log2(max(self.nfft, 4096))))
        self.spec_freqs = core.spectrogram_freqs_khz(self.spec_nfft, self.fs, self.iq)
        self.spec_win = np.hanning(self.spec_nfft)
        self.peak = None

        cdtype = np.complex128 if self.iq else np.float64
        self.pending = np.empty(0, dtype=cdtype)
        self.recent = np.empty(0, dtype=cdtype)
        self.recent_max = max(self.spec_nfft * 2, int(self.fs * 0.1))

        self._build_ui()

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(max(10, int(args.refresh * 1000)))

    # -- UI construction --
    def _build_ui(self):
        pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
        area = DockArea()
        self.setCentralWidget(area)

        # Waterfall (with interactive level/colormap histogram)
        d_wf = Dock("Waterfall", size=(900, 520))
        glw = pg.GraphicsLayoutWidget()
        self.p_wf = glw.addPlot(row=0, col=0)
        self.p_wf.setLabel("bottom", "Time relative to now", "s")
        self.p_wf.setLabel("left", "Frequency", "kHz")
        self.p_wf.setMouseEnabled(x=False, y=True)
        self.img = pg.ImageItem()
        self.p_wf.addItem(self.img)
        # Prime the image with its initial (empty) array BEFORE setRect().
        # pyqtgraph 0.11.x computes the rect scale from self.width()/height(),
        # which are None until the item has data; 0.14 tolerated an empty item
        # but 0.11 raises. setImage first makes width()/height() valid.
        self.img.setImage(self.wf, autoLevels=False)
        y0, y1 = float(self.wf_freqs[0]), float(self.wf_freqs[-1])
        self.img.setRect(QtCore.QRectF(-self.args.wf_seconds, y0,
                                       self.args.wf_seconds, (y1 - y0)))
        self.hist = pg.HistogramLUTItem()
        self.hist.setImageItem(self.img)
        try:
            self.hist.gradient.loadPreset("thermal")
        except Exception:
            pass
        self.hist.setLevels(self.args.floor, self.args.ceil)
        self.img.setLevels((self.args.floor, self.args.ceil))
        glw.addItem(self.hist, row=0, col=1)
        d_wf.addWidget(glw)

        # Spectrum
        d_sp = Dock("Spectrum", size=(640, 300))
        glw2 = pg.GraphicsLayoutWidget()
        self.p_sp = glw2.addPlot()
        self.p_sp.setLabel("bottom", "Frequency", "kHz")
        self.p_sp.setLabel("left", "Magnitude", "dB")
        self.p_sp.showGrid(x=True, y=True, alpha=0.3)
        self.p_sp.setYRange(self.args.floor, self.args.ceil)
        self.curve_sp = self.p_sp.plot(pen=pg.mkPen((110, 200, 255), width=1))
        self.curve_pk = self.p_sp.plot(pen=pg.mkPen((255, 120, 120), width=1))
        d_sp.addWidget(glw2)

        # Waveform
        wlabel = "Waveform (I/Q)" if self.iq else "Waveform"
        d_w = Dock(wlabel, size=(640, 300))
        glw3 = pg.GraphicsLayoutWidget()
        self.p_w = glw3.addPlot()
        self.p_w.setLabel("bottom", "Time", "ms")
        self.p_w.setLabel("left", "Raw counts")
        self.p_w.showGrid(x=True, y=True, alpha=0.3)
        self.curve_w = self.p_w.plot(pen=pg.mkPen((90, 255, 140), width=1))
        self.curve_w2 = (self.p_w.plot(pen=pg.mkPen((255, 190, 60), width=1))
                         if self.iq else None)
        d_w.addWidget(glw3)

        # Link health
        d_h = Dock("Link health", size=(320, 300))
        panel = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(panel)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setVerticalSpacing(3)
        self.value_labels = {}
        rows = [
            ("sid", "Stream ID"),
            ("pps", "packets/s"),
            ("obs", "observed Sa/s"),
            ("est_fs", "ts-derived Fs"),
            ("packets", "packets"),
            ("seq_gaps", "seq gaps (4-bit)"),
            ("ts_drops", "ts drops (exact)"),
            ("reorders", "reorders"),
            ("glitches", "time glitches"),
            ("sample_loss", "sampleloss pkts"),
            ("parse_errors", "parse errors"),
        ]
        for i, (key, label) in enumerate(rows):
            k = QtWidgets.QLabel(label)
            k.setStyleSheet("color:%s;" % DIM)
            v = QtWidgets.QLabel("-")
            v.setStyleSheet("color:%s; font-weight:bold;" % GREEN)
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self.value_labels[key] = v

        self.status_label = QtWidgets.QLabel("starting...")
        self.status_label.setStyleSheet("color:%s;" % DIM)
        grid.addWidget(self.status_label, len(rows), 0, 1, 2)

        b_pause = QtWidgets.QPushButton("Pause")
        b_pause.setCheckable(True)
        b_pause.toggled.connect(self._toggle_pause)
        b_reset = QtWidgets.QPushButton("Reset counters")
        b_reset.clicked.connect(self.reader.reset_counters)
        b_peak = QtWidgets.QPushButton("Clear peak-hold")
        b_peak.clicked.connect(self._clear_peak)
        grid.addWidget(b_pause, len(rows) + 1, 0)
        grid.addWidget(b_reset, len(rows) + 1, 1)
        grid.addWidget(b_peak, len(rows) + 2, 0, 1, 2)
        grid.setRowStretch(len(rows) + 3, 1)
        d_h.addWidget(panel)

        area.addDock(d_wf, "left")
        area.addDock(d_h, "right")
        area.addDock(d_sp, "bottom", d_wf)
        area.addDock(d_w, "right", d_sp)

    # -- controls --
    def _toggle_pause(self, on):
        self.paused = on

    def _clear_peak(self):
        self.peak = None

    # -- per-frame update --
    def _tick(self):
        snap = self.reader.snapshot()
        self._update_health(snap)
        if self.paused:
            return

        new = self.reader.drain()
        if new is None or len(new) == 0:
            return

        # feed both buffers
        self.recent = np.concatenate([self.recent, new])
        if len(self.recent) > self.recent_max:
            self.recent = self.recent[-self.recent_max:]

        self.pending = np.concatenate([self.pending, new])
        # bound backlog so a GUI stall doesn't create a huge catch-up burst
        max_pending = self.hop * self.wf_cols + self.nfft
        if len(self.pending) > max_pending:
            self.pending = self.pending[-max_pending:]

        cols, self.pending = core.spectrogram_columns(
            self.pending, self.nfft, self.hop, self.iq, self.win)
        if cols.shape[1] > 0:
            m = cols.shape[1]
            if m >= self.wf_cols:
                self.wf[:] = cols[:, -self.wf_cols:]
            else:
                self.wf[:, :-m] = self.wf[:, m:]
                self.wf[:, -m:] = cols
            self.img.setImage(self.wf, autoLevels=False)

        self._update_spectrum()
        self._update_waveform()

    def _update_spectrum(self):
        if len(self.recent) < self.spec_nfft:
            return
        y = self.recent[-self.spec_nfft:]
        y = y - np.mean(y)
        if self.iq:
            sp = np.fft.fftshift(np.fft.fft(y * self.spec_win))
        else:
            sp = np.fft.rfft(y * self.spec_win)
        db = 20.0 * np.log10(np.abs(sp) + 1e-12)
        self.curve_sp.setData(self.spec_freqs, db)
        if self.peak is None or self.peak.shape != db.shape:
            self.peak = db.copy()
        else:
            np.maximum(self.peak, db, out=self.peak)
        self.curve_pk.setData(self.spec_freqs, self.peak)

    def _update_waveform(self):
        n = min(len(self.recent), max(1, int(self.fs * 0.020)))
        if n < 2:
            return
        seg = self.recent[-n:]
        t_ms = np.arange(n) / self.fs * 1000.0
        if self.iq:
            self.curve_w.setData(t_ms, seg.real)
            self.curve_w2.setData(t_ms, seg.imag)
        else:
            self.curve_w.setData(t_ms, np.real(seg))

    def _update_health(self, snap):
        def put(key, text, alarm=False):
            lab = self.value_labels[key]
            lab.setText(text)
            lab.setStyleSheet("color:%s; font-weight:bold;"
                              % (RED if alarm else GREEN))

        put("sid", str(snap["sid"]),
            alarm=(snap["sid"] not in ("0x42783031", "?")))
        put("pps", "%.1f" % snap["pps"])
        put("obs", "%.0f" % snap["obs"])
        put("est_fs", "%.0f" % snap["est_fs"] if snap["est_fs"] else "-")
        put("packets", "%d" % snap["packets"])
        for key in ("seq_gaps", "ts_drops", "reorders", "glitches",
                    "sample_loss", "parse_errors"):
            put(key, "%d" % snap[key], alarm=snap[key] > 0)

        if snap["error"]:
            self.status_label.setText("tshark error: %s" % snap["error"])
            self.status_label.setStyleSheet("color:%s;" % RED)
        elif snap["finished"]:
            self.status_label.setText("stream ended")
            self.status_label.setStyleSheet("color:%s;" % DIM)
        else:
            self.status_label.setText("receiving")
            self.status_label.setStyleSheet("color:%s;" % GREEN)

    def closeEvent(self, ev):
        try:
            self.timer.stop()
        except Exception:
            pass
        self.reader.stop()
        ev.accept()


# ---------------------------------------------------------------------------
def _add_common(p, live):
    p.add_argument("--group", default=core.GROUP)
    p.add_argument("--port", type=int, default=core.PORT)
    p.add_argument("--source", default=core.SOURCE)
    p.add_argument("--fs", type=float, default=core.FS)
    p.add_argument("--dtype", choices=list(core._DTYPES.keys()), default="be-i32")
    p.add_argument("--iq", action="store_true",
                   help="interpret samples as interleaved complex I/Q")
    p.add_argument("--nfft", type=int, default=1024)
    p.add_argument("--hop", type=int, default=256)
    p.add_argument("--wf-seconds", type=float, default=6.0,
                   help="waterfall time span")
    p.add_argument("--refresh", type=float, default=0.05,
                   help="GUI refresh period in seconds (0.05 = 20 fps)")
    p.add_argument("--floor", type=float, default=40.0,
                   help="initial waterfall/spectrum dB floor")
    p.add_argument("--ceil", type=float, default=140.0,
                   help="initial waterfall/spectrum dB ceiling")
    if live:
        p.add_argument("--interface", default=core.IFACE)
        p.add_argument("--src-mac", default=core.SRC_MAC,
                       help="tighten BPF with ether src; '' to disable")
        p.add_argument("--src-port", type=int, default=core.SRC_PORT,
                       help="tighten display filter with src port; 0 to disable")
    else:
        p.add_argument("pcap")


def main():
    ap = argparse.ArgumentParser(
        description="RS-34 Bx VITA-49 real-time dashboard (passive)")
    sp = ap.add_subparsers(dest="mode")
    _add_common(sp.add_parser("live-tshark"), live=True)
    _add_common(sp.add_parser("pcap"), live=False)

    args = ap.parse_args()
    if args.mode is None:
        ap.print_help()
        raise SystemExit(2)
    if args.mode == "live-tshark":
        args.mode = "live-tshark"  # explicit; reader checks != "pcap"

    print("RS-34 Bx VITA-49 DASHBOARD (passive)")
    print("Run as your normal desktop user, NOT with sudo.")
    if args.mode != "pcap":
        print("If capture permission fails, run `sudo -v` and retry.")
    print("")

    app = QtWidgets.QApplication(sys.argv)
    reader = Reader(args)
    atexit.register(reader.stop)
    reader.start()

    win = Dashboard(reader, args)
    win.show()

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl-C kill the app
    ret = app.exec_() if hasattr(app, "exec_") else app.exec()
    reader.stop()
    sys.exit(ret)


if __name__ == "__main__":
    main()
