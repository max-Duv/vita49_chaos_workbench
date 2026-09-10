#!/usr/bin/env python3
"""Passive live-path diagnostic for the VITA-49 Chaos Workbench.

Receives only; never emits or mutates traffic.  It is deliberately GUI-free so
multicast membership, fragment reassembly, tshark fallback, and VRT parsing can
be checked independently of Qt.
"""
from __future__ import print_function

import argparse
import binascii
import time

from sources import AutoLiveSource, MulticastSource, TsharkLiveSource
from vita49 import VRTFrame, decode_payload


def main():
    ap = argparse.ArgumentParser(description="Probe VITA-49 live receive path")
    ap.add_argument("--group", default="239.254.253.252")
    ap.add_argument("--port", type=int, default=52101)
    ap.add_argument("--interface", default="bridge0")
    ap.add_argument("--capture-backend", choices=["auto","socket","tshark"], default="auto")
    ap.add_argument("--tshark-sudo", choices=["auto","always","never"], default="auto")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--dtype", default="be-i32")
    ap.add_argument("--iq", action="store_true")
    args = ap.parse_args()

    if args.capture_backend == "socket":
        src = MulticastSource(args.group, args.port, args.interface)
    elif args.capture_backend == "tshark":
        src = TsharkLiveSource(args.group, args.port, args.interface, args.tshark_sudo)
    else:
        src = AutoLiveSource(args.group, args.port, args.interface, 1.5, args.tshark_sudo)

    print("VITA-49 live receive probe (passive)")
    print("target: %s:%d  interface=%s  requested-backend=%s" %
          (args.group, args.port, args.interface, args.capture_backend))
    print("duration: %.1fs" % args.seconds)
    print("")

    start = time.monotonic()
    packets = 0
    bytes_rx = 0
    parsed = 0
    decoded_samples = 0
    first = True
    try:
        src.open()
        while time.monotonic() - start < args.seconds:
            ev = src.read(0.10)
            if ev is None:
                continue
            packets += 1
            bytes_rx += len(ev.raw)
            try:
                f = VRTFrame.parse(ev.raw)
                x = decode_payload(f, args.dtype, args.iq)
                parsed += 1
                decoded_samples += len(x)
                if first:
                    first = False
                    print("FIRST DATAGRAM")
                    print("  backend       : %s" % ev.source)
                    print("  UDP payload   : %d bytes" % len(ev.raw))
                    print("  first 32 bytes: %s" % binascii.hexlify(ev.raw[:32]).decode("ascii"))
                    print("  packet type   : %d" % f.packet_type)
                    print("  packet count  : %d" % f.packet_count)
                    print("  declared words: %d (%d bytes)" %
                          (f.packet_size_words, f.packet_size_words * 4))
                    print("  stream id     : %s" %
                          ("0x%08X" % f.stream_id if f.stream_id is not None else "none"))
                    print("  TSI / TSF     : %d / %d" % (f.tsi, f.tsf))
                    print("  payload bytes : %d" % len(f.payload_bytes()))
                    print("  decoded samples (%s%s): %d" %
                          (args.dtype, ", IQ" if args.iq else "", len(x)))
                    print("")
            except Exception as e:
                if first:
                    first = False
                    print("FIRST DATAGRAM RECEIVED BUT VRT PARSE/DECODE FAILED")
                    print("  backend       : %s" % ev.source)
                    print("  UDP payload   : %d bytes" % len(ev.raw))
                    print("  first 64 bytes: %s" % binascii.hexlify(ev.raw[:64]).decode("ascii"))
                    print("  error         : %s" % e)
                    print("")
    finally:
        try:
            src.close()
        except Exception:
            pass

    elapsed = max(1e-6, time.monotonic() - start)
    print("RESULT")
    print("  datagrams     : %d" % packets)
    print("  receive rate  : %.2f packets/s" % (packets / elapsed))
    print("  bytes         : %d" % bytes_rx)
    print("  VRT parsed    : %d" % parsed)
    print("  decoded samp. : %d" % decoded_samples)
    try:
        print("  source status : %r" % src.status())
    except Exception:
        pass
    if packets == 0:
        print("")
        print("NO DATAGRAMS REACHED THE application receive path.")
        print("If tcpdump still sees the multicast, rerun with --capture-backend tshark after `sudo -v`.")


if __name__ == "__main__":
    main()
