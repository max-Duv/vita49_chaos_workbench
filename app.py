#!/usr/bin/env python3

import argparse
import os
import signal
import sys

from PyQt5 import QtCore, QtWidgets

from gui import WorkbenchWindow
from sources import (AutoLiveSource, DemoSource, MulticastEmitter,
                     MulticastSource, PcapSource, TsharkLiveSource)
from worker import WorkbenchWorker


def build_parser():
    ap=argparse.ArgumentParser(description="VITA-49 Chaos Engineering Workbench")
    ap.add_argument("mode",choices=["demo","live","pcap"],nargs="?",default="demo")
    ap.add_argument("pcap",nargs="?",default="")
    ap.add_argument("--group",default="239.254.253.252",help="input multicast group")
    ap.add_argument("--port",type=int,default=52102,help="input UDP port")
    ap.add_argument("--interface",default="0.0.0.0",help="input interface name or IPv4 address")
    ap.add_argument("--capture-backend", choices=["auto","socket","tshark"], default="auto",
                    help="live capture backend; auto tries kernel multicast then fragment-aware tshark")
    ap.add_argument("--live-fallback-seconds", type=float, default=1.5,
                    help="seconds auto mode waits for socket packets before switching to tshark")
    ap.add_argument("--tshark-sudo", choices=["auto","always","never"], default="auto",
                    help="whether tshark capture should use sudo -n")
    ap.add_argument("--out-group",default="239.255.77.77",help="TEST output multicast group")
    ap.add_argument("--out-port",type=int,default=52102,help="TEST output UDP port")
    ap.add_argument("--out-interface",default="0.0.0.0",help="output interface name or IPv4 address")
    ap.add_argument("--ttl",type=int,default=1)
    ap.add_argument("--emit",action="store_true",help="check emit box by default; still requires ARM + RUN")
    ap.add_argument("--fs",type=float,default=250000.0)
    ap.add_argument("--dtype",choices=["be-i8","be-i16","be-i32","be-f32","be-f64","le-i8","le-i16","le-i32","le-f32","le-f64"],default="be-i16")
    ap.add_argument("--iq",action="store_true")
    ap.add_argument("--packet-samples",type=int,default=512,help="demo mode")
    ap.add_argument("--demo-tone-hz",type=float,default=18000.0)
    ap.add_argument("--demo-amplitude",type=float,default=12000.0)
    ap.add_argument("--stream-id",type=lambda x:int(x,0),default=0x42783031)
    ap.add_argument("--pcap-speed",type=float,default=1.0)
    ap.add_argument("--nfft",type=int,default=1024)
    ap.add_argument("--hop",type=int,default=256)
    ap.add_argument("--wf-seconds",type=float,default=6.0)
    ap.add_argument("--refresh",type=float,default=.05)
    ap.add_argument("--floor",type=float,default=40.0)
    ap.add_argument("--ceil",type=float,default=140.0)
    ap.add_argument("--seed",type=int,default=1337)
    ap.add_argument("--log-dir",default="logs")
    return ap


def main():
    args=build_parser().parse_args()
    if args.mode=="pcap" and not args.pcap:
        raise SystemExit("pcap mode requires a PCAP path")
    if args.mode=="live":
        if args.capture_backend == "socket":
            source=MulticastSource(args.group,args.port,args.interface)
        elif args.capture_backend == "tshark":
            source=TsharkLiveSource(args.group,args.port,args.interface,args.tshark_sudo)
        else:
            source=AutoLiveSource(args.group,args.port,args.interface,
                                  args.live_fallback_seconds,args.tshark_sudo)
    elif args.mode=="pcap":
        source=PcapSource(args.pcap,args.group,args.port,args.pcap_speed)
    else:
        source=DemoSource(args.fs,args.packet_samples,args.dtype,args.iq,args.stream_id,args.demo_tone_hz,args.demo_amplitude)
    emitter=MulticastEmitter(args.out_group,args.out_port,args.out_interface,args.ttl)
    worker=WorkbenchWorker(source,emitter,args.fs,args.dtype,args.iq,args.seed,args.log_dir)
    worker.start()
    signal.signal(signal.SIGINT,signal.SIG_DFL)
    # Let Qt scale correctly on RHEL remote desktops / high-DPI sessions.
    try:
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    app=QtWidgets.QApplication(sys.argv)
    win=WorkbenchWindow(worker,args); win.show()
    rc=app.exec_(); worker.stop(); worker.join(timeout=1.0); sys.exit(rc)


if __name__=="__main__":
    main()
