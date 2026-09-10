#!/usr/bin/env python3

import os
import time

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from faults import default_config
from scenarios import PRESETS

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BG = "#080c11"
PANEL = "#0f151d"
PANEL_2 = "#131b24"
PANEL_3 = "#19232e"
BORDER = "#273442"
TEXT = "#e8eef5"
DIM = "#8d9aaa"
ACCENT = "#6c7cff"
ACCENT_2 = "#00d4ff"
GREEN = "#59e391"
RED = "#ff667a"
AMBER = "#ffbf69"
CYAN = "#62d4ff"


class MetricCard(QtWidgets.QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setMinimumWidth(112)
        self.setMaximumHeight(72)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(1)
        self.title = QtWidgets.QLabel(title.upper())
        self.title.setObjectName("metricTitle")
        self.value = QtWidgets.QLabel("—")
        self.value.setObjectName("metricValue")
        self.sub = QtWidgets.QLabel("")
        self.sub.setObjectName("metricSub")
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set_metric(self, value, sub="", alarm=False, warning=False):
        self.value.setText(str(value))
        self.sub.setText(str(sub))
        color = RED if alarm else (AMBER if warning else TEXT)
        self.value.setStyleSheet("color:%s;" % color)


class SectionHeader(QtWidgets.QFrame):
    def __init__(self, title, subtitle="", parent=None):
        super().__init__(parent)
        self.setObjectName("sectionHeader")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(8)
        title_lab = QtWidgets.QLabel(title)
        title_lab.setObjectName("sectionTitle")
        lay.addWidget(title_lab)
        if subtitle:
            sub = QtWidgets.QLabel(subtitle)
            sub.setObjectName("sectionSubtitle")
            lay.addWidget(sub)
        lay.addStretch(1)


class WorkbenchWindow(QtWidgets.QMainWindow):
    def __init__(self, worker, args):
        super().__init__()
        self.worker = worker
        self.args = args
        self.fs = float(args.fs)
        self.iq = bool(args.iq)
        self.nfft = int(args.nfft)
        self.hop = int(args.hop)
        self.wf_seconds = float(args.wf_seconds)
        self.cfg = default_config()
        self.armed = False
        self.running_exp = False

        sample_dtype = np.complex128 if self.iq else np.float64
        self.recent_in = np.empty(0, dtype=sample_dtype)
        self.recent_out = np.empty(0, dtype=sample_dtype)
        self.pending = np.empty(0, dtype=sample_dtype)
        self.win = np.hanning(self.nfft)
        self.wf_cols = max(256, int(self.wf_seconds * self.fs / self.hop))
        self.nbins = self.nfft if self.iq else self.nfft // 2 + 1
        self.wf = np.full((self.nbins, self.wf_cols), -140.0, np.float32)
        self.freqs = (np.fft.fftshift(np.fft.fftfreq(self.nfft, 1.0 / self.fs)) if self.iq
                      else np.fft.rfftfreq(self.nfft, 1.0 / self.fs)) / 1000.0
        self._event_display_count = 0

        self._build_ui()
        self._load_cfg_to_widgets(self.cfg)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(max(20, int(args.refresh * 1000)))

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pg.setConfigOptions(imageAxisOrder="row-major", antialias=False,
                            background=BG, foreground=DIM)
        self.setWindowTitle("VITA-49 Chaos Engineering Workbench")
        self.setMinimumSize(1024, 680)
        self.setStyleSheet(self._stylesheet())

        # Fit the actual remote desktop instead of assuming a 1580x960 display.
        app = QtWidgets.QApplication.instance()
        screen = app.primaryScreen() if app is not None else None
        if screen is not None:
            r = screen.availableGeometry()
            w = min(1500, max(1024, int(r.width() * 0.94)))
            h = min(940, max(680, int(r.height() * 0.90)))
            self.resize(w, h)
        else:
            self.resize(1280, 780)

        root = QtWidgets.QWidget()
        root.setObjectName("root")
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)

        outer.addWidget(self._top_bar())

        body = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        body.setObjectName("mainSplitter")
        body.setChildrenCollapsible(False)
        body.setHandleWidth(1)
        outer.addWidget(body, 1)

        sidebar = self._control_panel()
        sidebar.setMinimumWidth(300)
        sidebar.setMaximumWidth(390)
        body.addWidget(sidebar)

        workspace = self._workspace()
        body.addWidget(workspace)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([335, 1040])

        self.status = QtWidgets.QLabel(" PASS-THROUGH  •  Arm the experiment to enable the chaos path")
        self.status.setObjectName("bottomStatus")
        self.status.setMinimumHeight(24)
        outer.addWidget(self.status)

    def _top_bar(self):
        bar = QtWidgets.QFrame()
        bar.setObjectName("topBar")
        bar.setMinimumHeight(58)
        bar.setMaximumHeight(58)
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(16, 8, 14, 8)
        lay.setSpacing(12)

        brand = QtWidgets.QVBoxLayout()
        brand.setSpacing(0)
        title = QtWidgets.QLabel("VITA-49  /  CHAOS WORKBENCH")
        title.setObjectName("appTitle")
        subtitle = QtWidgets.QLabel("RF transport resilience • protocol faults • signal-path degradation")
        subtitle.setObjectName("appSubtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        lay.addLayout(brand)
        lay.addStretch(1)

        self.source_chip = QtWidgets.QLabel(" %s  %s:%d " %
                                            (self.args.mode.upper(), self.args.group, self.args.port))
        self.source_chip.setObjectName("chip")
        lay.addWidget(self.source_chip)

        self.mode_chip = QtWidgets.QLabel(" PASS-THROUGH ")
        self.mode_chip.setObjectName("stateChip")
        lay.addWidget(self.mode_chip)

        self.top_arm = QtWidgets.QPushButton("ARM")
        self.top_arm.setObjectName("armButton")
        self.top_arm.setCheckable(True)
        self.top_arm.toggled.connect(self._arm)
        self.top_arm.setMinimumWidth(72)
        lay.addWidget(self.top_arm)

        self.top_run = QtWidgets.QPushButton("RUN")
        self.top_run.setObjectName("runButton")
        self.top_run.setEnabled(False)
        self.top_run.clicked.connect(self._run_experiment)
        self.top_run.setMinimumWidth(72)
        lay.addWidget(self.top_run)

        self.top_stop = QtWidgets.QPushButton("STOP")
        self.top_stop.setObjectName("stopButton")
        self.top_stop.clicked.connect(self._stop_experiment)
        self.top_stop.setMinimumWidth(72)
        lay.addWidget(self.top_stop)
        return bar

    def _workspace(self):
        wrap = QtWidgets.QWidget()
        wrap.setObjectName("workspace")
        lay = QtWidgets.QVBoxLayout(wrap)
        lay.setContentsMargins(10, 10, 10, 8)
        lay.setSpacing(8)

        # Telemetry strip remains visible on every workspace tab.
        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(7)
        self.cards = {}
        for key, title in [
                ("pps", "Packets / s"),
                ("samples", "Samples / s"),
                ("seq", "Seq gaps"),
                ("ts", "TS drops"),
                ("glitch", "Glitches"),
                ("parser", "Parse errors"),
                ("dropped", "Chaos drops")]:
            c = MetricCard(title)
            self.cards[key] = c
            cards.addWidget(c, 1)
        lay.addLayout(cards)

        self.workspace_tabs = QtWidgets.QTabWidget()
        self.workspace_tabs.setObjectName("workspaceTabs")
        self.workspace_tabs.addTab(self._live_view(), "LIVE ANALYSIS")
        self.workspace_tabs.addTab(self._health_view(), "HEALTH + SCORECARD")
        self.workspace_tabs.addTab(self._events_view(), "FAULT EVENTS")
        lay.addWidget(self.workspace_tabs, 1)
        return wrap

    def _live_view(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(0, 7, 0, 0)
        lay.setSpacing(7)

        # Waterfall gets the majority of vertical space.
        wf_card = QtWidgets.QFrame()
        wf_card.setObjectName("plotCard")
        wfl = QtWidgets.QVBoxLayout(wf_card)
        wfl.setContentsMargins(0, 0, 0, 0)
        wfl.setSpacing(0)
        wfl.addWidget(SectionHeader("CHAOS WATERFALL", "mutated output stream"))
        glw = pg.GraphicsLayoutWidget()
        self.p_wf = glw.addPlot(row=0, col=0)
        self._style_plot(self.p_wf)
        self.p_wf.setLabel("bottom", "Time relative to now", "s")
        self.p_wf.setLabel("left", "Frequency", "kHz")
        self.p_wf.setMouseEnabled(x=False, y=True)
        self.img = pg.ImageItem()
        self.img.setImage(self.wf, autoLevels=False)
        y0, y1 = float(self.freqs[0]), float(self.freqs[-1])
        self.img.setRect(QtCore.QRectF(-self.wf_seconds, y0,
                                       self.wf_seconds, y1 - y0))
        self.img.setLevels((self.args.floor, self.args.ceil))
        self.p_wf.addItem(self.img)
        try:
            cmap = pg.ColorMap(
                np.array([0.0, 0.22, 0.50, 0.76, 1.0]),
                np.array([[5, 8, 18, 255], [24, 38, 96, 255],
                          [25, 162, 184, 255], [252, 174, 48, 255],
                          [255, 245, 193, 255]], dtype=np.ubyte))
            self.img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        except Exception:
            pass
        wfl.addWidget(glw, 1)
        lay.addWidget(wf_card, 3)

        lower = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        lower.setChildrenCollapsible(False)
        lower.setHandleWidth(5)

        spec_card = QtWidgets.QFrame(); spec_card.setObjectName("plotCard")
        spl = QtWidgets.QVBoxLayout(spec_card); spl.setContentsMargins(0,0,0,0); spl.setSpacing(0)
        spl.addWidget(SectionHeader("SPECTRUM", "clean vs chaos"))
        g2 = pg.GraphicsLayoutWidget()
        self.p_sp = g2.addPlot()
        self._style_plot(self.p_sp)
        self.p_sp.setLabel("bottom", "Frequency", "kHz")
        self.p_sp.setLabel("left", "Magnitude", "dB")
        self.p_sp.showGrid(x=True, y=True, alpha=.14)
        self.p_sp.addLegend(offset=(8, 8))
        clean_pen = pg.mkPen(CYAN, width=1)
        clean_pen.setStyle(QtCore.Qt.DashLine)
        self.sp_in = self.p_sp.plot(pen=clean_pen, name="Clean")
        self.sp_out = self.p_sp.plot(pen=pg.mkPen(RED, width=1.4), name="Chaos")
        spl.addWidget(g2, 1)
        lower.addWidget(spec_card)

        wave_card = QtWidgets.QFrame(); wave_card.setObjectName("plotCard")
        wvl = QtWidgets.QVBoxLayout(wave_card); wvl.setContentsMargins(0,0,0,0); wvl.setSpacing(0)
        wvl.addWidget(SectionHeader("WAVEFORM", "4 ms window • clean vs chaos"))
        g3 = pg.GraphicsLayoutWidget()
        self.p_wave = g3.addPlot()
        self._style_plot(self.p_wave)
        self.p_wave.setLabel("bottom", "Time", "ms")
        self.p_wave.setLabel("left", "Sample")
        self.p_wave.showGrid(x=True, y=True, alpha=.14)
        clean_wave_pen = pg.mkPen(CYAN, width=1)
        clean_wave_pen.setStyle(QtCore.Qt.DashLine)
        self.wave_in = self.p_wave.plot(pen=clean_wave_pen)
        self.wave_out = self.p_wave.plot(pen=pg.mkPen(RED, width=1.2))
        wvl.addWidget(g3, 1)
        lower.addWidget(wave_card)
        lower.setSizes([520, 520])
        lay.addWidget(lower, 2)

        tl_card = QtWidgets.QFrame(); tl_card.setObjectName("plotCard")
        tll = QtWidgets.QVBoxLayout(tl_card); tll.setContentsMargins(0,0,0,0); tll.setSpacing(0)
        tll.addWidget(SectionHeader("FAULT TIMELINE", "last 1000 injected events"))
        gt = pg.GraphicsLayoutWidget()
        self.p_tl = gt.addPlot()
        self._style_plot(self.p_tl)
        self.p_tl.setMaximumHeight(118)
        self.p_tl.setLabel("bottom", "Experiment time", "s")
        self.p_tl.setYRange(-0.5, 5.5)
        self.p_tl.getAxis("left").setTicks([[
            (0, "DROP"), (1, "SEQ"), (2, "TIME"),
            (3, "NET"), (4, "SIGNAL"), (5, "OTHER")]])
        self.timeline_scatter = pg.ScatterPlotItem(
            size=7, brush=pg.mkBrush(255, 102, 122, 210), pen=None)
        self.p_tl.addItem(self.timeline_scatter)
        tll.addWidget(gt, 1)
        lay.addWidget(tl_card, 1)
        return page

    def _health_view(self):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        # Clean/chaos scorecard.
        card = QtWidgets.QFrame(); card.setObjectName("contentCard")
        cl = QtWidgets.QVBoxLayout(card); cl.setContentsMargins(0,0,0,0); cl.setSpacing(0)
        cl.addWidget(SectionHeader("STREAM HEALTH", "baseline compared with chaos output"))
        table_wrap = QtWidgets.QWidget(); g = QtWidgets.QGridLayout(table_wrap)
        g.setContentsMargins(14, 12, 14, 14); g.setHorizontalSpacing(18); g.setVerticalSpacing(8)
        h0=QtWidgets.QLabel("METRIC"); h1=QtWidgets.QLabel("CLEAN"); h2=QtWidgets.QLabel("CHAOS")
        for h in (h0,h1,h2): h.setObjectName("tableHeader")
        g.addWidget(h0,0,0); g.addWidget(h1,0,1); g.addWidget(h2,0,2)
        self.health = {}
        metrics = [("sid","Stream ID"),("pps","Packets / s"),("obs","Samples / s"),
                   ("mbps","Mbit / s"),("packets","Packets"),("seq_gaps","Sequence gaps"),
                   ("reorders","Reorders"),("ts_drops","Timestamp drops"),
                   ("glitches","Time glitches"),("parse_errors","Parse errors")]
        for r,(key,label) in enumerate(metrics,1):
            lab=QtWidgets.QLabel(label); lab.setObjectName("tableMetric")
            a=QtWidgets.QLabel("—"); b=QtWidgets.QLabel("—")
            a.setObjectName("tableValue"); b.setObjectName("tableValue")
            g.addWidget(lab,r,0); g.addWidget(a,r,1); g.addWidget(b,r,2)
            self.health[key]=(a,b)
        g.setColumnStretch(0, 2); g.setColumnStretch(1, 1); g.setColumnStretch(2, 1)
        g.setRowStretch(len(metrics)+1,1)
        cl.addWidget(table_wrap, 1)
        lay.addWidget(card, 3)

        side = QtWidgets.QVBoxLayout(); side.setSpacing(8)
        eng = QtWidgets.QFrame(); eng.setObjectName("contentCard")
        el = QtWidgets.QVBoxLayout(eng); el.setContentsMargins(0,0,0,0); el.setSpacing(0)
        el.addWidget(SectionHeader("CHAOS ENGINE", "experiment counters"))
        ew = QtWidgets.QWidget(); eg=QtWidgets.QGridLayout(ew); eg.setContentsMargins(14,10,14,14); eg.setSpacing(8)
        self.engine_values={}
        for r,(key,label) in enumerate([("seen","Seen"),("emitted","Emitted"),("dropped","Dropped"),
                                        ("duplicates","Duplicated"),("mutated","Mutated"),("reordered","Reordered")]):
            l=QtWidgets.QLabel(label); l.setObjectName("tableMetric")
            v=QtWidgets.QLabel("0"); v.setObjectName("bigSideValue")
            eg.addWidget(l,r,0); eg.addWidget(v,r,1); self.engine_values[key]=v
        el.addWidget(ew)
        side.addWidget(eng)

        logc=QtWidgets.QFrame(); logc.setObjectName("contentCard")
        ll=QtWidgets.QVBoxLayout(logc); ll.setContentsMargins(0,0,0,0); ll.setSpacing(0)
        ll.addWidget(SectionHeader("EXPERIMENT LOG", "JSONL result stream"))
        self.log_label=QtWidgets.QLabel("No experiment log yet")
        self.log_label.setObjectName("logPath"); self.log_label.setWordWrap(True)
        ll.addWidget(self.log_label,1)
        side.addWidget(logc,1)
        lay.addLayout(side,2)
        return page

    def _events_view(self):
        page=QtWidgets.QWidget(); lay=QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(0,8,0,0); lay.setSpacing(8)
        card=QtWidgets.QFrame(); card.setObjectName("contentCard")
        cl=QtWidgets.QVBoxLayout(card); cl.setContentsMargins(0,0,0,0); cl.setSpacing(0)
        cl.addWidget(SectionHeader("FAULT EVENT STREAM", "most recent injected actions"))
        self.event_table=QtWidgets.QTableWidget(0,4)
        self.event_table.setHorizontalHeaderLabels(["T+ (s)","LANE","FAULT","DETAIL"])
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.event_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.event_table.setAlternatingRowColors(True)
        self.event_table.setShowGrid(False)
        hh=self.event_table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        cl.addWidget(self.event_table,1)
        lay.addWidget(card,1)
        return page

    def _control_panel(self):
        root=QtWidgets.QFrame(); root.setObjectName("sidebar")
        lay=QtWidgets.QVBoxLayout(root); lay.setContentsMargins(10,10,10,10); lay.setSpacing(8)

        heading=QtWidgets.QHBoxLayout()
        title=QtWidgets.QLabel("CHAOS CONTROL"); title.setObjectName("sideTitle")
        self.side_state=QtWidgets.QLabel("SAFE"); self.side_state.setObjectName("safeBadge")
        heading.addWidget(title); heading.addStretch(1); heading.addWidget(self.side_state)
        lay.addLayout(heading)

        exp=QtWidgets.QFrame(); exp.setObjectName("controlCard")
        el=QtWidgets.QVBoxLayout(exp); el.setContentsMargins(10,9,10,10); el.setSpacing(7)
        lab=QtWidgets.QLabel("SCENARIO"); lab.setObjectName("fieldLabel"); el.addWidget(lab)
        self.preset=QtWidgets.QComboBox(); self.preset.addItems(list(PRESETS.keys())); self.preset.currentTextChanged.connect(self._apply_preset)
        self.preset.setMaxVisibleItems(9); el.addWidget(self.preset)

        row=QtWidgets.QHBoxLayout(); row.setSpacing(7)
        seed_l=QtWidgets.QLabel("Seed"); seed_l.setObjectName("fieldLabel")
        self.seed=QtWidgets.QSpinBox(); self.seed.setRange(0,2147483647); self.seed.setValue(self.args.seed)
        row.addWidget(seed_l); row.addWidget(self.seed,1); el.addLayout(row)

        self.emit=QtWidgets.QCheckBox("Emit mutated stream to TEST multicast")
        self.emit.setChecked(bool(self.args.emit)); el.addWidget(self.emit)
        lay.addWidget(exp)

        self.tabs=QtWidgets.QTabWidget(); self.tabs.setObjectName("faultTabs")
        self.tabs.addTab(self._scroll_tab(self._transport_tab()),"TRANSPORT")
        self.tabs.addTab(self._scroll_tab(self._protocol_tab()),"VITA")
        self.tabs.addTab(self._scroll_tab(self._signal_tab()),"SIGNAL")
        self.tabs.addTab(self._scroll_tab(self._system_tab()),"SYSTEM")
        lay.addWidget(self.tabs,1)

        safety=QtWidgets.QFrame(); safety.setObjectName("safetyCard")
        sl=QtWidgets.QVBoxLayout(safety); sl.setContentsMargins(10,8,10,8); sl.setSpacing(3)
        st=QtWidgets.QLabel("SAFETY BOUNDARY"); st.setObjectName("fieldLabel"); sl.addWidget(st)
        inp=QtWidgets.QLabel("IN   %s:%d"%(self.args.group,self.args.port)); inp.setObjectName("smallMono"); sl.addWidget(inp)
        out=QtWidgets.QLabel("OUT  %s:%d"%(self.args.out_group,self.args.out_port)); out.setObjectName("smallMono"); sl.addWidget(out)
        n=QtWidgets.QLabel("Output is blocked if it matches the source destination."); n.setObjectName("safetyNote"); n.setWordWrap(True); sl.addWidget(n)
        lay.addWidget(safety)
        return root

    def _scroll_tab(self, widget):
        scroll=QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff); scroll.setWidget(widget)
        return scroll

    def _section_box(self, title):
        box=QtWidgets.QGroupBox(title); box.setObjectName("faultGroup")
        form=QtWidgets.QFormLayout(box); form.setContentsMargins(9,12,9,9); form.setSpacing(7)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        return box, form

    def _transport_tab(self):
        page=QtWidgets.QWidget(); lay=QtWidgets.QVBoxLayout(page); lay.setContentsMargins(3,7,3,7); lay.setSpacing(8)
        b,f=self._section_box("PACKET DELIVERY")
        self.drop=self._dspin(0,100,.5); f.addRow("Random drop %",self.drop)
        self.dup=self._dspin(0,100,.5); f.addRow("Duplicate %",self.dup)
        self.blackout=QtWidgets.QCheckBox("Total blackout"); f.addRow(self.blackout)
        lay.addWidget(b)
        b,f=self._section_box("BURST LOSS")
        self.burst=QtWidgets.QCheckBox("Enable burst loss"); f.addRow(self.burst)
        self.burst_n=self._spin(0,100000); f.addRow("Drop packets",self.burst_n)
        self.burst_every=self._spin(0,1000000); f.addRow("Every N packets",self.burst_every)
        lay.addWidget(b)
        b,f=self._section_box("IMPAIRMENT")
        self.delay=self._dspin(0,10000,1); f.addRow("Fixed delay ms",self.delay)
        self.jitter=self._dspin(0,10000,1); f.addRow("Jitter sigma ms",self.jitter)
        self.reorder=self._dspin(0,100,.5); f.addRow("Reorder %",self.reorder)
        self.reorder_win=self._spin(2,128); f.addRow("Reorder window",self.reorder_win)
        self.throttle=self._dspin(0,1000000,10); f.addRow("Throttle packets/s",self.throttle)
        lay.addWidget(b); lay.addStretch(1); return page

    def _protocol_tab(self):
        page=QtWidgets.QWidget(); lay=QtWidgets.QVBoxLayout(page); lay.setContentsMargins(3,7,3,7); lay.setSpacing(8)
        b,f=self._section_box("SEQUENCE")
        self.seq_mode=QtWidgets.QComboBox(); self.seq_mode.addItems(["off","jump","freeze","random"]); f.addRow("Mode",self.seq_mode)
        self.seq_jump=self._spin(-15,15); f.addRow("Jump",self.seq_jump)
        self.seq_every=self._spin(0,1000000); f.addRow("Every N packets",self.seq_every)
        lay.addWidget(b)
        b,f=self._section_box("STREAM ID")
        self.sid_enable=QtWidgets.QCheckBox("Mutate stream ID"); f.addRow(self.sid_enable)
        self.sid_value=QtWidgets.QLineEdit("0xDEADBEEF"); f.addRow("Value",self.sid_value); lay.addWidget(b)
        b,f=self._section_box("TIMESTAMP")
        self.ts_offset=self._dspin(-60000,60000,1,3); f.addRow("Offset ms",self.ts_offset)
        self.ts_drift=self._dspin(-100000,100000,1,3); f.addRow("Drift ppm",self.ts_drift)
        self.ts_jitter=self._dspin(0,1000000,1,3); f.addRow("Jitter us",self.ts_jitter)
        self.ts_freeze=QtWidgets.QCheckBox("Freeze timestamps"); f.addRow(self.ts_freeze)
        self.ts_step=self._dspin(-60000,60000,1,3); f.addRow("Periodic step ms",self.ts_step)
        self.ts_step_every=self._spin(0,1000000); f.addRow("Step every N",self.ts_step_every); lay.addWidget(b)
        b,f=self._section_box("CORRUPTION")
        self.trunc_pct=self._dspin(0,100,.1); f.addRow("Truncate %",self.trunc_pct)
        self.trunc_bytes=self._spin(1,4096); f.addRow("Remove bytes",self.trunc_bytes)
        self.keep_size=QtWidgets.QCheckBox("Keep declared packet size stale"); f.addRow(self.keep_size)
        self.header_flip=self._dspin(0,100,.1); f.addRow("Header bit-flip %",self.header_flip)
        self.payload_flip=self._dspin(0,100,.1); f.addRow("Payload bit-flip %",self.payload_flip)
        self.payload_bits=self._spin(1,1024); f.addRow("Bits per affected pkt",self.payload_bits); lay.addWidget(b)
        lay.addStretch(1); return page

    def _signal_tab(self):
        page=QtWidgets.QWidget(); lay=QtWidgets.QVBoxLayout(page); lay.setContentsMargins(3,7,3,7); lay.setSpacing(8)
        b,f=self._section_box("SIGNAL PATH")
        self.sig_enable=QtWidgets.QCheckBox("Enable signal-domain faults"); f.addRow(self.sig_enable)
        self.gain=self._dspin(-10,10,.05,3); f.addRow("Gain x",self.gain)
        self.dc=self._dspin(-1e9,1e9,10,3); f.addRow("DC offset",self.dc)
        self.snr=self._dspin(0,120,.5,2); f.addRow("AWGN target SNR dB",self.snr)
        self.clip=self._dspin(0,1e9,100,3); f.addRow("Clip |sample|",self.clip); lay.addWidget(b)
        b,f=self._section_box("SAMPLE FAULTS")
        self.zero=self._dspin(0,100,.5); f.addRow("Zero packet %",self.zero)
        self.sample_drop=self._dspin(0,100,.1); f.addRow("Sample dropout %",self.sample_drop)
        self.stuck_en=QtWidgets.QCheckBox("Stuck-sample packet"); f.addRow(self.stuck_en)
        self.stuck_value=self._dspin(-1e9,1e9,100,3); f.addRow("Stuck value",self.stuck_value); lay.addWidget(b)
        b,f=self._section_box("INTERFERENCE + I/Q")
        self.tone_en=QtWidgets.QCheckBox("Inject test tone"); f.addRow(self.tone_en)
        self.tone_hz=self._dspin(-1e9,1e9,100,2); f.addRow("Tone Hz",self.tone_hz)
        self.tone_amp=self._dspin(-1e9,1e9,100,2); f.addRow("Tone amplitude",self.tone_amp)
        self.iq_swap=QtWidgets.QCheckBox("I/Q swap"); f.addRow(self.iq_swap)
        self.iq_conj=QtWidgets.QCheckBox("I/Q conjugate"); f.addRow(self.iq_conj)
        self.phase=self._dspin(-3600,3600,1,2); f.addRow("Phase rotation deg",self.phase); lay.addWidget(b)
        lay.addStretch(1); return page

    def _system_tab(self):
        page=QtWidgets.QWidget(); lay=QtWidgets.QVBoxLayout(page); lay.setContentsMargins(3,7,3,7); lay.setSpacing(8)
        b,f=self._section_box("CONSUMER PATH")
        self.proc_delay=self._dspin(0,5000,1); f.addRow("Per-packet delay ms",self.proc_delay)
        self.stall_every=self._spin(0,1000000); f.addRow("Stall every N packets",self.stall_every)
        self.stall_ms=self._dspin(0,60000,10); f.addRow("Stall duration ms",self.stall_ms)
        lay.addWidget(b)
        info=QtWidgets.QLabel("These faults delay the workbench consumer path only. They do not generate external host-wide CPU load.")
        info.setObjectName("infoBox"); info.setWordWrap(True); lay.addWidget(info); lay.addStretch(1); return page

    def _dspin(self, lo, hi, step=1.0, decimals=2):
        w=QtWidgets.QDoubleSpinBox(); w.setRange(lo,hi); w.setSingleStep(step); w.setDecimals(decimals); w.setMinimumHeight(28); return w

    def _spin(self, lo, hi):
        w=QtWidgets.QSpinBox(); w.setRange(lo,hi); w.setMinimumHeight(28); return w

    def _style_plot(self, plot):
        plot.setMenuEnabled(False)
        plot.getViewBox().setBorder(pg.mkPen(BORDER, width=1))
        for axis_name in ("left","bottom"):
            axis=plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(BORDER))
            axis.setTextPen(pg.mkPen(DIM))

    # --------------------------------------------------------- configuration
    def _collect_cfg(self):
        c=default_config(); t=c["transport"]; p=c["protocol"]; s=c["signal"]; y=c["system"]
        t.update(drop_pct=self.drop.value(),burst_enabled=self.burst.isChecked(),
                 burst_drop_packets=self.burst_n.value(),burst_every_packets=self.burst_every.value(),
                 blackout=self.blackout.isChecked(),duplicate_pct=self.dup.value(),
                 fixed_delay_ms=self.delay.value(),jitter_ms=self.jitter.value(),
                 reorder_pct=self.reorder.value(),reorder_window=self.reorder_win.value(),
                 throttle_pps=self.throttle.value())
        try: sid=int(self.sid_value.text().strip(),0)
        except Exception: sid=0xDEADBEEF
        p.update(seq_mode=self.seq_mode.currentText(),seq_jump=self.seq_jump.value(),
                 seq_every_packets=self.seq_every.value(),sid_enabled=self.sid_enable.isChecked(),
                 sid_value=sid,ts_offset_ms=self.ts_offset.value(),ts_drift_ppm=self.ts_drift.value(),
                 ts_jitter_us=self.ts_jitter.value(),ts_freeze=self.ts_freeze.isChecked(),
                 ts_step_ms=self.ts_step.value(),ts_step_every_packets=self.ts_step_every.value(),
                 truncate_pct=self.trunc_pct.value(),truncate_bytes=self.trunc_bytes.value(),
                 truncate_keep_size=self.keep_size.isChecked(),header_bitflip_pct=self.header_flip.value(),
                 payload_bitflip_pct=self.payload_flip.value(),payload_bitflip_bits=self.payload_bits.value())
        s.update(enabled=self.sig_enable.isChecked(),gain=self.gain.value(),dc_offset=self.dc.value(),
                 noise_snr_db=self.snr.value(),clip_abs=self.clip.value(),zero_pct=self.zero.value(),
                 sample_dropout_pct=self.sample_drop.value(),stuck_enabled=self.stuck_en.isChecked(),
                 stuck_value=self.stuck_value.value(),tone_enabled=self.tone_en.isChecked(),
                 tone_hz=self.tone_hz.value(),tone_amp=self.tone_amp.value(),
                 iq_swap=self.iq_swap.isChecked(),iq_conjugate=self.iq_conj.isChecked(),
                 phase_deg=self.phase.value())
        y.update(processing_delay_ms=self.proc_delay.value(),consumer_stall_every=self.stall_every.value(),
                 consumer_stall_ms=self.stall_ms.value())
        return c

    def _load_cfg_to_widgets(self,c):
        t=c["transport"]; p=c["protocol"]; s=c["signal"]; y=c["system"]
        self.drop.setValue(t["drop_pct"]); self.burst.setChecked(t["burst_enabled"]); self.burst_n.setValue(t["burst_drop_packets"]); self.burst_every.setValue(t["burst_every_packets"]); self.blackout.setChecked(t["blackout"]); self.dup.setValue(t["duplicate_pct"]); self.delay.setValue(t["fixed_delay_ms"]); self.jitter.setValue(t["jitter_ms"]); self.reorder.setValue(t["reorder_pct"]); self.reorder_win.setValue(t["reorder_window"]); self.throttle.setValue(t["throttle_pps"])
        self.seq_mode.setCurrentText(p["seq_mode"]); self.seq_jump.setValue(p["seq_jump"]); self.seq_every.setValue(p["seq_every_packets"]); self.sid_enable.setChecked(p["sid_enabled"]); self.sid_value.setText("0x%08X"%p["sid_value"]); self.ts_offset.setValue(p["ts_offset_ms"]); self.ts_drift.setValue(p["ts_drift_ppm"]); self.ts_jitter.setValue(p["ts_jitter_us"]); self.ts_freeze.setChecked(p["ts_freeze"]); self.ts_step.setValue(p["ts_step_ms"]); self.ts_step_every.setValue(p["ts_step_every_packets"]); self.trunc_pct.setValue(p["truncate_pct"]); self.trunc_bytes.setValue(p["truncate_bytes"]); self.keep_size.setChecked(p["truncate_keep_size"]); self.header_flip.setValue(p["header_bitflip_pct"]); self.payload_flip.setValue(p["payload_bitflip_pct"]); self.payload_bits.setValue(p["payload_bitflip_bits"])
        self.sig_enable.setChecked(s["enabled"]); self.gain.setValue(s["gain"]); self.dc.setValue(s["dc_offset"]); self.snr.setValue(s["noise_snr_db"]); self.clip.setValue(s["clip_abs"]); self.zero.setValue(s["zero_pct"]); self.sample_drop.setValue(s["sample_dropout_pct"]); self.stuck_en.setChecked(s["stuck_enabled"]); self.stuck_value.setValue(s["stuck_value"]); self.tone_en.setChecked(s["tone_enabled"]); self.tone_hz.setValue(s["tone_hz"]); self.tone_amp.setValue(s["tone_amp"]); self.iq_swap.setChecked(s["iq_swap"]); self.iq_conj.setChecked(s["iq_conjugate"]); self.phase.setValue(s["phase_deg"])
        self.proc_delay.setValue(y["processing_delay_ms"]); self.stall_every.setValue(y["consumer_stall_every"]); self.stall_ms.setValue(y["consumer_stall_ms"])

    def _apply_preset(self,name):
        fn=PRESETS.get(name)
        if fn:
            self.cfg=fn(); self._load_cfg_to_widgets(self.cfg)

    # ------------------------------------------------------------- controls
    def _arm(self,on):
        self.armed=bool(on)
        self.top_run.setEnabled(self.armed)
        # Avoid signal recursion while mirroring the top-state button.
        self.top_arm.blockSignals(True); self.top_arm.setChecked(on); self.top_arm.blockSignals(False)
        self.top_arm.setText("ARMED" if on else "ARM")
        if on:
            self.side_state.setText("ARMED"); self.side_state.setObjectName("armedBadge")
            self.mode_chip.setText(" ARMED ")
        else:
            self.side_state.setText("SAFE"); self.side_state.setObjectName("safeBadge")
            if not self.running_exp: self.mode_chip.setText(" PASS-THROUGH ")
        self.side_state.style().unpolish(self.side_state); self.side_state.style().polish(self.side_state)

    def _run_experiment(self):
        if not self.armed: return
        if self.emit.isChecked() and (self.args.group==self.args.out_group and self.args.port==self.args.out_port):
            QtWidgets.QMessageBox.critical(self,"Emission blocked",
                "Input and test-output group/port are identical. Choose a separate test destination.")
            return
        cfg=self._collect_cfg()
        self.worker.begin_experiment(cfg,self.seed.value(),self.emit.isChecked(),
            {"source_mode":self.args.mode,"input":"%s:%d"%(self.args.group,self.args.port),
             "output":"%s:%d"%(self.args.out_group,self.args.out_port)})
        self.running_exp=True
        self.mode_chip.setText(" CHAOS ACTIVE ")
        self.mode_chip.setStyleSheet("color:%s;border-color:%s;"%(RED,RED))
        self.status.setText(" CHAOS ACTIVE  •  deterministic seed %d  •  %s"%
                            (self.seed.value(),self.preset.currentText()))

    def _stop_experiment(self):
        self.worker.stop_experiment(); self.running_exp=False
        self.top_arm.setChecked(False)
        self.mode_chip.setText(" PASS-THROUGH "); self.mode_chip.setStyleSheet("")
        self.status.setText(" PASS-THROUGH  •  chaos engine stopped")

    # -------------------------------------------------------------- updates
    def _tick(self):
        snap=self.worker.snapshot(); self._update_health(snap)
        src=snap.get("source",{}) or {}
        backend=str(src.get("backend","live"))
        if self.args.mode == "live":
            self.source_chip.setText(" LIVE  %s  %s:%d " %
                                     (backend.upper(),self.args.group,self.args.port))
            tip=("Interface: %s\nBackend: %s\nReceived datagrams: %s\n" %
                 (src.get("interface",self.args.interface), backend,
                  src.get("rx_packets",0)))
            if src.get("join"):
                tip += "Membership: %s\n" % src.get("join")
            if src.get("interface_ipv4"):
                tip += "Interface IPv4: %s\n" % src.get("interface_ipv4")
            if src.get("fallback_reason"):
                tip += "Fallback: %s\n" % src.get("fallback_reason")
            self.source_chip.setToolTip(tip.rstrip())
        if snap["error"]:
            self.status.setText(" ERROR  •  "+snap["error"])
            self.status.setStyleSheet("color:%s;"%RED)
        elif (self.args.mode == "live" and snap.get("input",{}).get("packets",0)==0
              and not self.running_exp):
            msg=" WAITING FOR VITA-49  •  %s on %s"%(backend,self.args.interface)
            if src.get("fallback_reason"):
                msg += "  •  "+str(src.get("fallback_reason"))
            self.status.setText(msg)
            self.status.setStyleSheet("color:%s;"%AMBER)
        elif not self.running_exp:
            self.status.setText(" PASS-THROUGH  •  receiving via %s"%backend)
            self.status.setStyleSheet("")
        xi,xo=self.worker.drain_samples()
        keep=max(4*self.nfft,int(.2*self.fs))
        if len(xi): self.recent_in=np.concatenate([self.recent_in,xi])[-keep:]
        if len(xo):
            self.recent_out=np.concatenate([self.recent_out,xo])[-keep:]
            self.pending=np.concatenate([self.pending,xo]); self._update_waterfall()
        self._update_spectrum(); self._update_wave(); self._update_timeline(); self._update_event_table()

    def _spec(self,x):
        if len(x)<self.nfft: return None
        y=x[-self.nfft:]-np.mean(x[-self.nfft:]); sp=np.fft.fft(y*self.win)
        if self.iq: sp=np.fft.fftshift(sp)
        else: sp=sp[:self.nfft//2+1]
        return 20*np.log10(np.abs(sp)+1e-12)

    def _update_spectrum(self):
        a=self._spec(self.recent_in); b=self._spec(self.recent_out)
        if a is not None: self.sp_in.setData(self.freqs,a)
        if b is not None: self.sp_out.setData(self.freqs,b)

    def _update_wave(self):
        # 4 ms is readable at RF sample rates; 20 ms rendered as a solid block.
        n=min(len(self.recent_in),len(self.recent_out),max(2,int(.004*self.fs)))
        if n<2:return
        t=np.arange(n)/self.fs*1000.0
        ain=self.recent_in[-n:]; aout=self.recent_out[-n:]
        if np.iscomplexobj(ain): ain=np.real(ain)
        if np.iscomplexobj(aout): aout=np.real(aout)
        # Keep remote-desktop drawing cost bounded.
        step=max(1,int(n/1600))
        self.wave_in.setData(t[::step],ain[::step]); self.wave_out.setData(t[::step],aout[::step])

    def _update_waterfall(self):
        cols=[]
        while len(self.pending)>=self.nfft:
            y=self.pending[:self.nfft]-np.mean(self.pending[:self.nfft]); sp=np.fft.fft(y*self.win)
            if self.iq: sp=np.fft.fftshift(sp)
            else: sp=sp[:self.nfft//2+1]
            cols.append(20*np.log10(np.abs(sp)+1e-12).astype(np.float32)); self.pending=self.pending[self.hop:]
            if len(cols)>80: break
        if not cols:return
        c=np.stack(cols,axis=1); m=c.shape[1]
        if m>=self.wf_cols:self.wf[:]=c[:,-self.wf_cols:]
        else:self.wf[:,:-m]=self.wf[:,m:]; self.wf[:,-m:]=c
        self.img.setImage(self.wf,autoLevels=False)

    def _fmt(self,v):
        if v is None:return "—"
        if isinstance(v,float):
            if abs(v)>=100000:return "%.0f"%v
            if abs(v)>=1000:return "%.1f"%v
            return "%.2f"%v
        return str(v)

    def _update_health(self,snap):
        inp=snap["input"]; out=snap["output"]; e=snap["engine"]
        self.cards["pps"].set_metric(self._fmt(out.get("pps")),"clean %s"%self._fmt(inp.get("pps")))
        self.cards["samples"].set_metric(self._fmt(out.get("obs")),"clean %s"%self._fmt(inp.get("obs")))
        self.cards["seq"].set_metric(self._fmt(out.get("seq_gaps")),"sequence",alarm=(out.get("seq_gaps",0)>0))
        self.cards["ts"].set_metric(self._fmt(out.get("ts_drops")),"timestamp",alarm=(out.get("ts_drops",0)>0))
        self.cards["glitch"].set_metric(self._fmt(out.get("glitches")),"timing",alarm=(out.get("glitches",0)>0))
        self.cards["parser"].set_metric(self._fmt(out.get("parse_errors")),"decoder",alarm=(out.get("parse_errors",0)>0))
        self.cards["dropped"].set_metric(self._fmt(e.get("dropped",0)),"injected",warning=(e.get("dropped",0)>0))

        for key,(a,b) in self.health.items():
            va=inp.get(key); vb=out.get(key); a.setText(self._fmt(va)); b.setText(self._fmt(vb))
            alarm=key in ("seq_gaps","reorders","ts_drops","glitches","parse_errors") and isinstance(vb,(int,float)) and vb>0
            b.setStyleSheet("color:%s;font-weight:700;"%(RED if alarm else GREEN))
        for key,lab in self.engine_values.items(): lab.setText(self._fmt(e.get(key,0)))
        self.log_label.setText(snap.get("log_path") or "No experiment log yet")

    def _event_lane(self,name):
        low=name.lower()
        if "drop" in low or "blackout" in low:return 0,"DROP"
        if "sequence" in low:return 1,"SEQ"
        if "timestamp" in low:return 2,"TIME"
        if "reorder" in low or "jitter" in low or "duplicate" in low or "throttle" in low:return 3,"NET"
        if any(k in low for k in ("awgn","gain","tone","clip","iq_","phase","sample_","zero_","dc_")):return 4,"SIGNAL"
        return 5,"OTHER"

    def _update_timeline(self):
        ev=self.worker.get_events()
        if not ev:return
        t0=self.worker.started_wall or ev[0][0]; pts=[]
        for ts,name,_ in ev[-1000:]:
            lane,_=self._event_lane(name)
            pts.append({"pos":(ts-t0,lane),"data":name,"tip":name})
        self.timeline_scatter.setData(pts)
        if self.running_exp:
            now=max(1.0,time.time()-t0); self.p_tl.setXRange(max(0,now-30),max(30,now),padding=0)

    def _update_event_table(self):
        ev=self.worker.get_events()
        if len(ev)==self._event_display_count:return
        self._event_display_count=len(ev)
        show=ev[-250:]
        t0=self.worker.started_wall or (show[0][0] if show else time.time())
        self.event_table.setUpdatesEnabled(False); self.event_table.setRowCount(len(show))
        for r,(ts,name,detail) in enumerate(show):
            _,lane=self._event_lane(name)
            vals=("%.3f"%(ts-t0),lane,name,detail)
            for c,val in enumerate(vals):
                item=QtWidgets.QTableWidgetItem(str(val)); self.event_table.setItem(r,c,item)
        self.event_table.scrollToBottom(); self.event_table.setUpdatesEnabled(True)

    def _stylesheet(self):
        return """
        QMainWindow, QWidget#root { background:%s; color:%s; }
        QWidget { color:%s; font-family:'DejaVu Sans','Liberation Sans',sans-serif; font-size:10pt; }
        QFrame#topBar { background:#0b1118; border-bottom:1px solid %s; }
        QLabel#appTitle { color:%s; font-size:13pt; font-weight:800; letter-spacing:1px; }
        QLabel#appSubtitle { color:%s; font-size:8.5pt; }
        QLabel#chip, QLabel#stateChip { background:%s; border:1px solid %s; border-radius:10px; padding:4px 8px; color:%s; font-size:8.5pt; font-weight:600; }
        QPushButton { background:%s; color:%s; border:1px solid %s; border-radius:5px; padding:6px 10px; font-weight:700; }
        QPushButton:hover { background:#223041; border-color:#40536a; }
        QPushButton:disabled { color:#536171; background:#111820; border-color:#202b37; }
        QPushButton#armButton:checked { background:#59313a; color:#ffdce2; border-color:%s; }
        QPushButton#runButton:enabled { background:#243d35; color:#caffdf; border-color:#38705a; }
        QPushButton#stopButton { color:#ffc9d0; }
        QFrame#sidebar { background:#0b1016; border-right:1px solid %s; }
        QLabel#sideTitle { font-size:10pt; font-weight:800; letter-spacing:1px; color:%s; }
        QLabel#safeBadge, QLabel#armedBadge { border-radius:8px; padding:2px 7px; font-size:8pt; font-weight:800; }
        QLabel#safeBadge { color:%s; background:#153326; border:1px solid #285d46; }
        QLabel#armedBadge { color:#ffd6dc; background:#4a222b; border:1px solid #793744; }
        QFrame#controlCard, QFrame#safetyCard, QFrame#contentCard, QFrame#plotCard, QFrame#metricCard { background:%s; border:1px solid %s; border-radius:6px; }
        QFrame#safetyCard { background:#12181f; }
        QLabel#fieldLabel, QLabel#metricTitle, QLabel#tableHeader { color:%s; font-size:7.8pt; font-weight:800; letter-spacing:.8px; }
        QLabel#metricValue { color:%s; font-size:14pt; font-weight:800; }
        QLabel#metricSub, QLabel#smallMono, QLabel#safetyNote { color:%s; font-size:8pt; }
        QLabel#smallMono, QLabel#logPath { font-family:'DejaVu Sans Mono','Liberation Mono',monospace; }
        QLabel#safetyNote { color:%s; }
        QFrame#sectionHeader { background:#111923; border:0; border-bottom:1px solid %s; }
        QLabel#sectionTitle { color:%s; font-size:8pt; font-weight:800; letter-spacing:1px; }
        QLabel#sectionSubtitle { color:%s; font-size:8pt; }
        QLabel#tableMetric { color:#aab6c4; }
        QLabel#tableValue { color:%s; font-family:'DejaVu Sans Mono','Liberation Mono',monospace; font-weight:700; }
        QLabel#bigSideValue { color:%s; font-size:13pt; font-weight:800; }
        QLabel#logPath { color:#a9b7c6; padding:12px; }
        QLabel#infoBox { background:#111923; border:1px solid %s; border-radius:5px; color:%s; padding:9px; }
        QLabel#bottomStatus { background:#0b1118; color:%s; border-top:1px solid %s; padding-left:9px; font-size:8.5pt; }
        QTabWidget::pane { border:1px solid %s; background:%s; border-radius:5px; }
        QTabBar::tab { background:#111820; color:%s; border:0; border-bottom:2px solid transparent; padding:8px 11px; margin-right:1px; font-size:8pt; font-weight:700; }
        QTabBar::tab:selected { color:%s; border-bottom:2px solid %s; background:#151e28; }
        QTabBar::tab:hover { color:%s; background:#151e28; }
        QTabWidget#faultTabs QTabBar::tab { padding:7px 6px; font-size:7.5pt; }
        QGroupBox#faultGroup { background:%s; border:1px solid %s; border-radius:5px; margin-top:8px; padding-top:7px; font-weight:700; color:#c9d4e0; }
        QGroupBox#faultGroup::title { subcontrol-origin:margin; left:8px; padding:0 5px; color:%s; font-size:7.7pt; }
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background:#161f29; color:%s; border:1px solid #2a3948; border-radius:4px; padding:4px 6px; selection-background-color:%s; min-height:20px; }
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border:1px solid %s; }
        QComboBox::drop-down { border:0; width:22px; }
        QComboBox QAbstractItemView, QAbstractItemView { background:#101720; color:%s; border:1px solid #344455; selection-background-color:#263a56; selection-color:white; outline:0; }
        QCheckBox { color:#c8d2dd; spacing:7px; }
        QCheckBox::indicator { width:15px; height:15px; border-radius:3px; border:1px solid #415266; background:#111820; }
        QCheckBox::indicator:checked { background:%s; border-color:%s; }
        QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; border:0; }
        QScrollBar:vertical { background:#0d131a; width:9px; margin:0; }
        QScrollBar::handle:vertical { background:#2b3948; min-height:30px; border-radius:4px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        QSplitter::handle { background:%s; }
        QTableWidget { background:%s; alternate-background-color:#111923; border:0; color:#d8e1ea; selection-background-color:#23364f; }
        QHeaderView::section { background:#111923; color:%s; border:0; border-bottom:1px solid %s; padding:7px; font-size:8pt; font-weight:800; }
        QToolTip { background:#121a23; color:white; border:1px solid #405167; padding:5px; }
        """ % (BG,TEXT,TEXT,BORDER,TEXT,DIM,PANEL_2,BORDER,TEXT,PANEL_3,TEXT,BORDER,RED,BORDER,TEXT,GREEN,
                 PANEL,BORDER,DIM,TEXT,DIM,AMBER,BORDER,TEXT,DIM,TEXT,GREEN,BORDER,DIM,DIM,BORDER,BORDER,PANEL,
                 DIM,TEXT,ACCENT,TEXT,PANEL_2,BORDER,ACCENT_2,TEXT,ACCENT,ACCENT,TEXT,ACCENT,ACCENT,BORDER,PANEL,
                 DIM,BORDER)

    def closeEvent(self,ev):
        try:self.timer.stop()
        except Exception:pass
        self.worker.stop(); ev.accept()
