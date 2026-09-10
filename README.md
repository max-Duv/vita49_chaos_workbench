# VITA-49 Chaos Engineering Workbench

A GUI-driven lab workbench for controlled fault injection against a VITA-49/VRT UDP stream. The workbench keeps the original stream as the clean baseline, applies deterministic chaos to a copy, visualizes both, scores stream-health effects, and can optionally re-emit the mutated copy to a **separate test multicast group**.

## Capabilities

**Transport faults**: random loss, periodic burst loss, blackout, duplication, fixed delay, Gaussian jitter, packet reordering, and packet-rate throttling.

**VITA protocol faults**: packet-count jump/freeze/randomization, Stream-ID mutation, timestamp offset/drift/jitter/freeze/periodic steps, packet truncation, bounded header bit flips, and payload bit flips.

**Signal/payload faults**: gain error, DC offset, AWGN at a target SNR, clipping, zeroed packets, sample dropout, stuck-sample faults, injected tone, I/Q swap, I/Q conjugation, and phase rotation.

**System/consumer faults**: controlled per-packet processing delay and periodic consumer stalls. These intentionally slow the workbench path instead of creating host-wide CPU stress.

**Experiment tooling**: deterministic RNG seed, arm/run/stop workflow, built-in scenario presets, clean-vs-chaos health counters, fault timeline, live waterfall/spectrum/waveform, and JSONL logs.

## Safety boundary

The default output is `239.255.77.77:52102` with multicast TTL 1. Emission is OFF by default and requires both **ARM** and **RUN CHAOS**. The GUI blocks emission when the input multicast group/port exactly matches the output group/port. Keep the test output on an isolated lab VLAN or receiver chain you control.

## Install

Python 3.6+ is supported. **On Python 3.6, use the included installer rather than invoking the host's old `pip` directly.** Enterprise Linux Python 3.6 installations commonly ship pip 9.x, which is too old to recognize PyQt5's `manylinux2014` wheel and therefore attempts an unsupported source build.

```bash
python3 -m venv .venv
source .venv/bin/activate
./install.sh
```

For Python 3.6, `install.sh` first pins `pip==21.3.1`, `setuptools==59.6.0`, and `wheel==0.37.1`, then installs runtime dependencies from binary wheels only. The Qt stack is pinned to PyQt5 5.15.2 / Qt 5.15.2 / PyQt5-sip 12.9.1, along with pyqtgraph 0.11.1 and NumPy 1.19.5.

If you already created `.venv` and saw `PyQt5-5.15.2.tar.gz` followed by `setup.py egg_info`, **you do not need to recreate the venv**. Leave it activated and run:

```bash
./install.sh
./run_demo.sh
```

PCAP mode additionally requires `tshark` in `PATH`.

## Fastest first run: no SDR/network hardware

```bash
python app.py demo --dtype be-i16 --fs 250000
```

For complex I/Q demo data:

```bash
python app.py demo --iq --dtype be-i16 --fs 250000
```

The GUI starts in pass-through mode. Select a preset, click **ARM**, then **RUN CHAOS**. You can test all internal fault/visualization functions without enabling multicast emission.

## Live multicast input

```bash
python app.py live \
  --group 239.254.253.252 --port 52102 \
  --interface eno8403 \
  --dtype be-i32 --fs 250000
```

If the stream is interleaved I/Q, add `--iq`.

To make test-stream emission available in the GUI:

```bash
python app.py live \
  --group 239.254.253.252 --port 52102 --interface eno8403 \
  --out-group 239.255.77.77 --out-port 52102 --out-interface eno8403 \
  --dtype be-i32 --fs 250000 --emit
```

The `--emit` flag only checks the GUI option initially; it does not transmit until the operator arms and starts an experiment.

## PCAP replay

```bash
python app.py pcap capture.pcap \
  --group 239.254.253.252 --port 52102 \
  --dtype be-i32 --fs 250000 --pcap-speed 1.0
```

Use `--pcap-speed 10` for 10x replay or `0.5` for half speed.

## Recommended experiment sequence

1. Run **Clean / pass-through** and verify input/output health parity.
2. Run **Packet path degradation** and validate sequence/reorder detection.
3. Run **Burst outage** and measure recovery behavior.
4. Run **Timing collapse** and validate timestamp alarms.
5. Run **RF degradation** and compare waterfall/spectrum changes.
6. Run **Consumer stall invariant** and confirm visualization/consumer stalls are distinguishable from VITA transport faults.
7. Run **Mixed failure** after individual fault signatures are understood.

## Logs

Each RUN creates `logs/vita49_chaos_YYYYMMDD_HHMMSS.jsonl` containing the seed/configuration, fault events, one-second metric snapshots, and final summary. That makes runs repeatable and suitable for later detection-latency/recovery analysis.

## Notes about VITA-49 parsing

The included parser is intentionally conservative. It understands common VRT packet geometry from the first header word and edits only fields whose offsets can be determined from the packet flags. Unknown packet types are passed through. Before using protocol-field mutation against a vendor-specific stream, first compare clean mode against a captured baseline and confirm packet geometry in Wireshark/tshark.


## Python 3.6 installer troubleshooting

If an older copy of `install.sh` prints `You must give at least one requirement` followed by `pip==21.3.1: command not found`, the shell line continuation was mangled. Run this directly inside the activated virtual environment:

```bash
python -m pip install --upgrade 'pip==21.3.1' 'setuptools==59.6.0' 'wheel==0.37.1'
python -m pip install --only-binary=:all: -r requirements.txt
./run_demo.sh
```

The current installer deliberately keeps the Python 3.6 packaging-tool install on one physical shell line to avoid that failure mode.

## RHEL / Linux bridge live-capture notes

Live mode now defaults to `--capture-backend auto`.

1. It first joins the multicast group using a normal UDP socket.  When an interface
   name such as `bridge0` is supplied on Linux, membership uses the interface index
   (`ip_mreqn`) rather than assuming the bridge owns an IPv4 address.
2. If the kernel socket receives no datagrams for 1.5 seconds, the workbench falls
   back to `tshark` automatically.
3. The tshark capture filter intentionally matches only the multicast destination
   address.  UDP-port filtering is applied after IPv4 reassembly, because non-first
   fragments do not contain the UDP header.

For a live RS-34-style stream:

```bash
sudo -v
python3 app.py live \
  --group 239.254.253.252 \
  --port 52101 \
  --interface bridge0 \
  --dtype be-i32 \
  --fs 250000 \
  --capture-backend auto
```

To test reception independently of Qt and the chaos engine:

```bash
sudo -v
python3 probe_live.py \
  --group 239.254.253.252 \
  --port 52101 \
  --interface bridge0 \
  --dtype be-i32 \
  --seconds 5
```

The probe is passive and never emits or mutates traffic.  It prints the active
capture backend, UDP payload size, VRT header geometry, Stream ID, sample decode
count, and receive packet rate.
