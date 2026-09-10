#!/usr/bin/env python3
"""Minimal, robust VITA-49/VRT packet helpers used by the chaos workbench.

The parser intentionally focuses on packet geometry needed for controlled lab
fault injection: packet count, stream ID, timestamps, payload, and packet size.
Unknown packet types are preserved byte-for-byte unless a selected fault targets
an understood field.
"""

import math
import struct
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

STREAM_ID_TYPES = {1, 3, 4, 5, 6, 7}

DTYPES = {
    "be-i8": np.dtype(">i1"),
    "be-i16": np.dtype(">i2"),
    "be-i32": np.dtype(">i4"),
    "be-f32": np.dtype(">f4"),
    "be-f64": np.dtype(">f8"),
    "le-i8": np.dtype("<i1"),
    "le-i16": np.dtype("<i2"),
    "le-i32": np.dtype("<i4"),
    "le-f32": np.dtype("<f4"),
    "le-f64": np.dtype("<f8"),
}


@dataclass
class VRTFrame:
    raw: bytearray
    packet_type: int
    class_id_present: bool
    trailer_present: bool
    tsi: int
    tsf: int
    packet_count: int
    packet_size_words: int
    stream_id_offset: Optional[int]
    class_id_offset: Optional[int]
    int_ts_offset: Optional[int]
    frac_ts_offset: Optional[int]
    payload_offset: int
    payload_end: int

    @classmethod
    def parse(cls, data: bytes) -> "VRTFrame":
        if len(data) < 4:
            raise ValueError("VRT packet shorter than 4-byte header")
        raw = bytearray(data)
        word0 = struct.unpack_from(">I", raw, 0)[0]
        ptype = (word0 >> 28) & 0xF
        c = bool((word0 >> 27) & 1)
        t = bool((word0 >> 26) & 1)
        tsi = (word0 >> 22) & 0x3
        tsf = (word0 >> 20) & 0x3
        pcount = (word0 >> 16) & 0xF
        psize = word0 & 0xFFFF

        declared_bytes = psize * 4 if psize else len(raw)
        logical_end = min(len(raw), declared_bytes)
        pos = 4
        sid_off = None
        class_off = None
        its_off = None
        fts_off = None

        if ptype in STREAM_ID_TYPES and pos + 4 <= logical_end:
            sid_off = pos
            pos += 4
        if c and pos + 8 <= logical_end:
            class_off = pos
            pos += 8
        if tsi != 0 and pos + 4 <= logical_end:
            its_off = pos
            pos += 4
        if tsf != 0 and pos + 8 <= logical_end:
            fts_off = pos
            pos += 8

        payload_end = logical_end - (4 if t and logical_end - pos >= 4 else 0)
        payload_end = max(pos, payload_end)
        return cls(raw, ptype, c, t, tsi, tsf, pcount, psize,
                   sid_off, class_off, its_off, fts_off, pos, payload_end)

    def bytes(self) -> bytes:
        return bytes(self.raw)

    def _word0(self) -> int:
        return struct.unpack_from(">I", self.raw, 0)[0]

    def _set_word0(self, word: int) -> None:
        struct.pack_into(">I", self.raw, 0, word & 0xFFFFFFFF)

    @property
    def stream_id(self) -> Optional[int]:
        if self.stream_id_offset is None or self.stream_id_offset + 4 > len(self.raw):
            return None
        return struct.unpack_from(">I", self.raw, self.stream_id_offset)[0]

    def set_stream_id(self, value: int) -> bool:
        if self.stream_id_offset is None or self.stream_id_offset + 4 > len(self.raw):
            return False
        struct.pack_into(">I", self.raw, self.stream_id_offset, value & 0xFFFFFFFF)
        return True

    def set_packet_count(self, value: int) -> None:
        word = self._word0()
        word = (word & ~(0xF << 16)) | ((value & 0xF) << 16)
        self._set_word0(word)
        self.packet_count = value & 0xF

    def set_packet_size_words(self, words: int) -> None:
        word = self._word0()
        word = (word & ~0xFFFF) | (words & 0xFFFF)
        self._set_word0(word)
        self.packet_size_words = words & 0xFFFF

    def payload_bytes(self) -> bytes:
        return bytes(self.raw[self.payload_offset:self.payload_end])

    def replace_payload(self, payload: bytes) -> None:
        before = self.raw[:self.payload_offset]
        trailer = self.raw[self.payload_end:] if self.trailer_present else bytearray()
        self.raw = bytearray(before) + bytearray(payload) + bytearray(trailer)
        while len(self.raw) % 4:
            if self.trailer_present and len(trailer) >= 4:
                # pad before trailer when a trailer exists
                idx = len(self.raw) - len(trailer)
                self.raw[idx:idx] = b"\x00"
            else:
                self.raw.append(0)
        self.payload_end = len(self.raw) - (len(trailer) if self.trailer_present else 0)
        self.set_packet_size_words(len(self.raw) // 4)

    def truncate(self, remove_bytes: int, keep_declared_size: bool = True) -> None:
        remove_bytes = max(0, int(remove_bytes))
        if remove_bytes <= 0 or len(self.raw) <= 4:
            return
        n = min(remove_bytes, len(self.raw) - 4)
        del self.raw[-n:]
        self.payload_end = min(self.payload_end, len(self.raw))
        if not keep_declared_size:
            self.set_packet_size_words((len(self.raw) + 3) // 4)

    def timestamp_seconds(self) -> Optional[float]:
        if self.int_ts_offset is None and self.frac_ts_offset is None:
            return None
        sec = 0.0
        if self.int_ts_offset is not None and self.int_ts_offset + 4 <= len(self.raw):
            sec += float(struct.unpack_from(">I", self.raw, self.int_ts_offset)[0])
        if self.frac_ts_offset is not None and self.frac_ts_offset + 8 <= len(self.raw):
            frac = struct.unpack_from(">Q", self.raw, self.frac_ts_offset)[0]
            # TSF=2 is real-time fractional seconds. For other modes this still
            # gives a stable normalized quantity for relative chaos experiments.
            sec += float(frac) / float(1 << 64)
        return sec

    def set_timestamp_seconds(self, value: float) -> bool:
        if self.int_ts_offset is None and self.frac_ts_offset is None:
            return False
        value = max(0.0, float(value))
        whole = int(math.floor(value))
        frac = value - whole
        if self.int_ts_offset is not None and self.int_ts_offset + 4 <= len(self.raw):
            struct.pack_into(">I", self.raw, self.int_ts_offset, whole & 0xFFFFFFFF)
        if self.frac_ts_offset is not None and self.frac_ts_offset + 8 <= len(self.raw):
            f = int(frac * float(1 << 64)) & 0xFFFFFFFFFFFFFFFF
            struct.pack_into(">Q", self.raw, self.frac_ts_offset, f)
        return True


def decode_payload(frame: VRTFrame, dtype_name: str, iq: bool):
    dt = DTYPES[dtype_name]
    payload = frame.payload_bytes()
    count = len(payload) // dt.itemsize
    if count <= 0:
        return np.empty(0, dtype=np.complex128 if iq else np.float64)
    arr = np.frombuffer(payload[:count * dt.itemsize], dtype=dt).astype(np.float64)
    if iq:
        if len(arr) < 2:
            return np.empty(0, dtype=np.complex128)
        if len(arr) % 2:
            arr = arr[:-1]
        arr = arr[0::2] + 1j * arr[1::2]
    return arr


def encode_payload(samples, dtype_name: str, iq: bool) -> bytes:
    dt = DTYPES[dtype_name]
    if iq:
        z = np.asarray(samples, dtype=np.complex128)
        arr = np.empty(z.size * 2, dtype=np.float64)
        arr[0::2] = z.real
        arr[1::2] = z.imag
    else:
        arr = np.asarray(samples, dtype=np.float64)

    if np.issubdtype(dt, np.integer):
        info = np.iinfo(dt)
        arr = np.clip(np.rint(arr), info.min, info.max)
    return arr.astype(dt).tobytes()


def build_demo_packet(samples, stream_id: int, packet_count: int,
                      dtype_name: str, iq: bool, timestamp: Optional[float] = None) -> bytes:
    """Build a simple IF Data Packet with Stream ID and real-time timestamps."""
    if timestamp is None:
        timestamp = time.time()
    tsi = 1  # UTC-style integer timestamp
    tsf = 2  # real-time fractional timestamp
    ptype = 1
    payload = encode_payload(samples, dtype_name, iq)
    pad = (-len(payload)) % 4
    payload += b"\x00" * pad
    total_words = 1 + 1 + 1 + 2 + len(payload) // 4
    word0 = ((ptype & 0xF) << 28) | ((tsi & 0x3) << 22) | ((tsf & 0x3) << 20)
    word0 |= ((packet_count & 0xF) << 16) | (total_words & 0xFFFF)
    whole = int(timestamp)
    frac = int((timestamp - whole) * float(1 << 64)) & 0xFFFFFFFFFFFFFFFF
    return (struct.pack(">I", word0) + struct.pack(">I", stream_id & 0xFFFFFFFF) +
            struct.pack(">I", whole & 0xFFFFFFFF) + struct.pack(">Q", frac) + payload)
