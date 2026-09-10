#!/usr/bin/env python3

import binascii
import math
import os
import select
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from vita49 import build_demo_packet


@dataclass
class PacketEvent:
    raw: bytes
    captured_at: float
    source: str = "input"


def interface_ipv4(name_or_ip: str) -> str:
    """Return an interface IPv4 address when one exists.

    A Linux bridge can legitimately have no IPv4 address.  Callers must not
    assume 0.0.0.0 means the named interface itself should be ignored.
    """
    if not name_or_ip:
        return "0.0.0.0"
    try:
        socket.inet_aton(name_or_ip)
        return name_or_ip
    except OSError:
        pass
    try:
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            req = struct.pack("256s", name_or_ip[:15].encode("ascii"))
            out = fcntl.ioctl(s.fileno(), 0x8915, req)  # SIOCGIFADDR
            return socket.inet_ntoa(out[20:24])
        finally:
            s.close()
    except Exception:
        return "0.0.0.0"


def interface_index(name_or_ip: str) -> int:
    if not name_or_ip or name_or_ip == "0.0.0.0":
        return 0
    try:
        socket.inet_aton(name_or_ip)
        return 0
    except OSError:
        pass
    try:
        return int(socket.if_nametoindex(name_or_ip))
    except Exception:
        return 0


class MulticastSource:
    """Kernel UDP multicast receiver.

    On Linux, when the user supplies an interface *name*, membership is joined
    with ip_mreqn and the interface index.  This is important for L2 bridges
    such as bridge0 that may not own an IPv4 address.  The kernel then performs
    normal IPv4 fragment reassembly before recvfrom() returns the UDP datagram.
    """
    def __init__(self, group: str, port: int, interface: str = "0.0.0.0",
                 rcvbuf: int = 8 * 1024 * 1024):
        self.group, self.port, self.interface = group, int(port), interface
        self.rcvbuf = int(rcvbuf)
        self.sock = None
        self.local_ip = "0.0.0.0"
        self.ifindex = 0
        self.join_method = "not-open"
        self.rx_packets = 0
        self.rx_bytes = 0
        self.opened_at = None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        except OSError:
            pass

        # Bind to the UDP port on all local addresses.  Multicast membership
        # below determines which group/interface is accepted.
        s.bind(("", self.port))

        self.local_ip = interface_ipv4(self.interface)
        self.ifindex = interface_index(self.interface)
        group_bin = socket.inet_aton(self.group)

        joined = False
        errors = []

        # Linux ip_mreqn = multicast addr, local addr, interface index.
        # This works even if bridge0 itself has no L3 address.
        if self.ifindex > 0:
            try:
                mreqn = struct.pack("=4s4si", group_bin,
                                    socket.inet_aton("0.0.0.0"), self.ifindex)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreqn)
                self.join_method = "ifindex:%d" % self.ifindex
                joined = True
            except OSError as e:
                errors.append("ip_mreqn=%s" % e)

        # Portable IPv4-address membership fallback.
        if not joined:
            try:
                mreq = group_bin + socket.inet_aton(self.local_ip)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self.join_method = "ipv4:%s" % self.local_ip
                joined = True
            except OSError as e:
                errors.append("ip_mreq=%s" % e)

        if not joined:
            s.close()
            raise RuntimeError("multicast join failed on %s: %s" %
                               (self.interface, "; ".join(errors)))

        s.settimeout(0.02)
        self.sock = s
        self.opened_at = time.monotonic()

    def read(self, timeout: float = 0.02) -> Optional[PacketEvent]:
        if self.sock is None:
            self.open()
        self.sock.settimeout(timeout)
        try:
            data, _addr = self.sock.recvfrom(65535)
            self.rx_packets += 1
            self.rx_bytes += len(data)
            return PacketEvent(data, time.time(), "multicast-socket")
        except socket.timeout:
            return None

    def status(self):
        return {
            "backend": "socket",
            "group": self.group,
            "port": self.port,
            "interface": self.interface,
            "interface_ipv4": self.local_ip,
            "ifindex": self.ifindex,
            "join": self.join_method,
            "rx_packets": self.rx_packets,
            "rx_bytes": self.rx_bytes,
        }

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None


class TsharkLiveSource:
    """Fragment-aware live capture using tshark.

    Capture filtering is intentionally only by destination multicast address,
    not UDP port.  Non-initial IPv4 fragments do not contain a UDP header; a
    BPF `udp dst port ...` capture filter would discard those fragments before
    Wireshark could reassemble the datagram.  Port selection is therefore a
    *display* filter after IPv4 reassembly.
    """
    def __init__(self, group: str, port: int, interface: str,
                 sudo_mode: str = "auto"):
        self.group, self.port, self.interface = group, int(port), interface
        self.sudo_mode = sudo_mode
        self.proc = None
        self.rx_packets = 0
        self.rx_bytes = 0
        self.command_mode = "not-open"
        self._tried_sudo = False
        self._stderr_cache = ""

    def _base_cmd(self, use_sudo=False):
        cmd = []
        if use_sudo:
            cmd += ["sudo", "-n"]
        cmd += [
            "tshark", "-l", "-n", "-i", self.interface,
            # Capture every fragment for this multicast destination.
            "-f", "dst host %s" % self.group,
            # Ensure IPv4 reassembly before UDP/VITA payload extraction.
            "-o", "ip.defragment:TRUE",
            "-Y", "ip.dst==%s && udp.dstport==%d" % (self.group, self.port),
            "-T", "fields", "-E", "separator=|", "-E", "occurrence=f",
            "-e", "frame.time_epoch", "-e", "udp.payload",
        ]
        return cmd

    def _spawn(self, use_sudo=False):
        if shutil.which("tshark") is None:
            raise RuntimeError("tshark not found; install Wireshark/tshark or use --capture-backend socket")
        if use_sudo and shutil.which("sudo") is None:
            raise RuntimeError("sudo not found for tshark capture")
        cmd = self._base_cmd(use_sudo)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     universal_newlines=True, bufsize=1)
        self.command_mode = "sudo-tshark" if use_sudo else "tshark"

    def open(self):
        use_sudo = self.sudo_mode == "always"
        self._tried_sudo = use_sudo
        self._spawn(use_sudo)

    def _restart_with_sudo(self, prior_error=""):
        try:
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None
        self._tried_sudo = True
        self._stderr_cache = prior_error
        self._spawn(True)

    def _read_ready_line(self, timeout):
        if self.proc is None:
            self.open()
        fd = self.proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], max(0.0, float(timeout)))
        if not ready:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().strip()
                if (self.sudo_mode == "auto" and not self._tried_sudo and
                        os.geteuid() != 0):
                    self._restart_with_sudo(err)
                    return None
                raise RuntimeError("%s exited: %s" %
                                   (self.command_mode, err or "unknown capture error"))
            return None
        line = self.proc.stdout.readline()
        if not line:
            return None
        return line

    def read(self, timeout: float = 0.02) -> Optional[PacketEvent]:
        while True:
            line = self._read_ready_line(timeout)
            if line is None:
                return None
            parts = line.rstrip("\n").split("|", 1)
            if len(parts) != 2 or not parts[1]:
                # tshark may print a matching frame with no extracted payload;
                # keep waiting rather than report a fake packet.
                timeout = 0.0
                continue
            try:
                ts = float(parts[0]) if parts[0] else time.time()
                raw = binascii.unhexlify(parts[1].replace(":", ""))
            except Exception:
                timeout = 0.0
                continue
            self.rx_packets += 1
            self.rx_bytes += len(raw)
            return PacketEvent(raw, ts, self.command_mode)

    def status(self):
        return {
            "backend": self.command_mode,
            "group": self.group,
            "port": self.port,
            "interface": self.interface,
            "rx_packets": self.rx_packets,
            "rx_bytes": self.rx_bytes,
            "note": "capture filter is group-only so fragmented UDP can be reassembled",
        }

    def close(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None


class AutoLiveSource:
    """Use the unprivileged kernel socket first, then tshark if it stays silent."""
    def __init__(self, group: str, port: int, interface: str,
                 fallback_after: float = 1.5, tshark_sudo: str = "auto"):
        self.group, self.port, self.interface = group, int(port), interface
        self.fallback_after = max(0.25, float(fallback_after))
        self.socket_source = MulticastSource(group, port, interface)
        self.tshark_source = TsharkLiveSource(group, port, interface, tshark_sudo)
        self.active = None
        self.started = None
        self.fallback_reason = ""

    def open(self):
        self.started = time.monotonic()
        try:
            self.socket_source.open()
            self.active = self.socket_source
        except Exception as e:
            self.fallback_reason = "socket open/join failed: %s" % e
            self.tshark_source.open()
            self.active = self.tshark_source

    def _fallback(self):
        if self.active is self.tshark_source:
            return
        self.fallback_reason = (
            "kernel multicast socket received no datagrams for %.1fs" %
            self.fallback_after)
        try:
            self.socket_source.close()
        except Exception:
            pass
        self.tshark_source.open()
        self.active = self.tshark_source

    def read(self, timeout: float = 0.02) -> Optional[PacketEvent]:
        if self.active is None:
            self.open()
        ev = self.active.read(timeout)
        if ev is not None:
            return ev
        if (self.active is self.socket_source and self.started is not None and
                self.socket_source.rx_packets == 0 and
                time.monotonic() - self.started >= self.fallback_after):
            self._fallback()
        return None

    def status(self):
        st = self.active.status() if self.active is not None else {
            "backend": "auto-starting", "group": self.group, "port": self.port,
            "interface": self.interface, "rx_packets": 0, "rx_bytes": 0,
        }
        st = dict(st)
        st["requested_backend"] = "auto"
        if self.fallback_reason:
            st["fallback_reason"] = self.fallback_reason
        return st

    def close(self):
        try:
            self.socket_source.close()
        except Exception:
            pass
        try:
            self.tshark_source.close()
        except Exception:
            pass


class PcapSource:
    """PCAP replay through tshark, preserving packet timing at a selectable speed."""
    def __init__(self, path: str, group: str = "", port: int = 0, speed: float = 1.0):
        self.path, self.group, self.port = path, group, int(port)
        self.speed = max(0.01, float(speed))
        self.proc = None
        self.first_capture = None
        self.first_wall = None
        self.pending = None
        self.rx_packets = 0
        self.rx_bytes = 0

    def open(self):
        if not os.path.exists(self.path):
            raise RuntimeError("PCAP not found: %s" % self.path)
        display = []
        if self.group:
            display.append("ip.dst==%s" % self.group)
        if self.port:
            display.append("udp.dstport==%d" % self.port)
        cmd = ["tshark", "-n", "-r", self.path, "-o", "ip.defragment:TRUE"]
        if display:
            cmd += ["-Y", " && ".join(display)]
        cmd += ["-T", "fields", "-E", "separator=|", "-E", "occurrence=f",
                "-e", "frame.time_epoch", "-e", "udp.payload"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True, bufsize=1)

    def _next_line(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            parts = line.strip().split("|", 1)
            if len(parts) != 2 or not parts[1]:
                continue
            try:
                ts = float(parts[0])
                raw = binascii.unhexlify(parts[1].replace(":", ""))
                return ts, raw
            except Exception:
                continue

    def read(self, timeout: float = 0.02) -> Optional[PacketEvent]:
        if self.proc is None:
            self.open()
        if self.pending is None:
            self.pending = self._next_line()
            if self.pending is None:
                return None
        cap_ts, raw = self.pending
        if self.first_capture is None:
            self.first_capture, self.first_wall = cap_ts, time.monotonic()
        due = self.first_wall + (cap_ts - self.first_capture) / self.speed
        if time.monotonic() < due:
            time.sleep(min(timeout, max(0.0, due - time.monotonic())))
            return None
        self.pending = None
        self.rx_packets += 1
        self.rx_bytes += len(raw)
        return PacketEvent(raw, cap_ts, "pcap")

    @property
    def finished(self):
        return self.proc is not None and self.pending is None and self.proc.poll() is not None

    def status(self):
        return {"backend": "pcap", "path": self.path,
                "rx_packets": self.rx_packets, "rx_bytes": self.rx_bytes}

    def close(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None


class DemoSource:
    def __init__(self, fs=250000.0, packet_samples=512, dtype="be-i16", iq=False,
                 stream_id=0x42783031, tone_hz=18000.0, amplitude=12000.0):
        self.fs = float(fs)
        self.packet_samples = int(packet_samples)
        self.dtype = dtype
        self.iq = bool(iq)
        self.stream_id = int(stream_id)
        self.tone_hz = float(tone_hz)
        self.amplitude = float(amplitude)
        self.seq = 0
        self.sample_index = 0
        self.next_due = time.monotonic()
        self.period = self.packet_samples / self.fs
        self.rx_packets = 0
        self.rx_bytes = 0

    def open(self):
        self.next_due = time.monotonic()

    def read(self, timeout=0.02):
        now = time.monotonic()
        if now < self.next_due:
            time.sleep(min(timeout, self.next_due - now))
            return None
        n = self.packet_samples
        t = (np.arange(n) + self.sample_index) / self.fs
        if self.iq:
            x = self.amplitude * np.exp(1j * 2 * np.pi * self.tone_hz * t)
        else:
            x = self.amplitude * np.sin(2 * np.pi * self.tone_hz * t)
        raw = build_demo_packet(x, self.stream_id, self.seq, self.dtype, self.iq, time.time())
        self.seq = (self.seq + 1) & 0xF
        self.sample_index += n
        self.next_due += self.period
        if self.next_due < time.monotonic() - 0.5:
            self.next_due = time.monotonic()
        self.rx_packets += 1
        self.rx_bytes += len(raw)
        return PacketEvent(raw, time.time(), "demo")

    @property
    def finished(self):
        return False

    def status(self):
        return {"backend": "demo", "rx_packets": self.rx_packets,
                "rx_bytes": self.rx_bytes}

    def close(self):
        pass


class MulticastEmitter:
    def __init__(self, group: str, port: int, interface: str = "0.0.0.0", ttl: int = 1):
        self.group, self.port, self.interface, self.ttl = group, int(port), interface, int(ttl)
        self.sock = None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", self.ttl))
        local_ip = interface_ipv4(self.interface)
        if local_ip != "0.0.0.0":
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        self.sock = s

    def send(self, raw: bytes):
        if self.sock is None:
            self.open()
        self.sock.sendto(raw, (self.group, self.port))

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
