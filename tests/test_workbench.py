import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
from vita49 import VRTFrame, build_demo_packet, decode_payload
from faults import ChaosEngine, default_config


def pkt(iq=False):
    x=np.arange(32,dtype=float)
    if iq: x=x+1j*(x+10)
    return build_demo_packet(x,0x42783031,3,"be-i16",iq,1000.25)


def test_parse_demo():
    f=VRTFrame.parse(pkt())
    assert f.packet_type==1
    assert f.stream_id==0x42783031
    assert f.packet_count==3
    assert abs(f.timestamp_seconds()-1000.25)<1e-6


def test_sequence_jump():
    e=ChaosEngine(250000,"be-i16",False,123)
    c=default_config(); c["protocol"].update(seq_mode="jump",seq_jump=4,seq_every_packets=1)
    e.config.set(c); e.set_active(True)
    out=e.ingest(pkt(),time.monotonic())
    if not out: out=e.poll(time.monotonic()+1)
    f=VRTFrame.parse(out[0].raw)
    assert f.packet_count==7


def test_signal_gain():
    e=ChaosEngine(250000,"be-i16",False,123)
    c=default_config(); c["signal"].update(enabled=True,gain=2.0)
    e.config.set(c); e.set_active(True)
    out=e.ingest(pkt(),time.monotonic())
    if not out: out=e.poll(time.monotonic()+1)
    f=VRTFrame.parse(out[0].raw)
    x=decode_payload(f,"be-i16",False)
    assert x[10]==20


def test_drop_all():
    e=ChaosEngine(250000,"be-i16",False,123)
    c=default_config(); c["transport"]["drop_pct"]=100.0
    e.config.set(c); e.set_active(True)
    out=e.ingest(pkt(),time.monotonic())
    assert out==[]
    assert e.stats["dropped"]==1
