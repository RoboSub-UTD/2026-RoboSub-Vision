#!/usr/bin/env python3
"""
video_check.py: find out where the ROV video's lag spikes and stutter come from.

Captures (or listens to) the RTP video stream on UDP 5000 and splits the problem into:
  - packets lost on the way             -> frames the interface has to throw away (stutter)
  - delay spikes on the network         -> late frames (lag spikes), usually Wi-Fi
  - uneven frames from the ROV itself   -> camera / Pi hiccups (exposure, USB, CPU)
  - laptop socket overflow              -> the interface didn't read fast enough
If the stream itself is clean, the stutter is in the interface (YOLO, CPU, display).

Works with MJPEG (payload 26) and H.264 (payload 96) streams. Standard library only;
draws a timeline chart if matplotlib is installed (it is in the vision .venv).

  # with the vision interface running (needs tcpdump: sudo dnf install tcpdump)
  python3 video_check.py capture --seconds 60

  # with the interface closed: listen on the port directly
  python3 video_check.py live --seconds 30

  # analyze an existing capture (tcpdump -w file.pcap udp port 5000)
  python3 video_check.py analyze file.pcap

Each run writes a folder video_check_<time>/ with summary.txt, frames.csv and timeline.png.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import os
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

CLOCK = 90000.0          # RTP clock for video (both JPEG and H.264)
SPIKE_MS = 50.0          # extra delay that starts to be noticeable
BAD_SPIKE_MS = 100.0     # extra delay that is clearly a lag spike
FREEZE_MS = 150.0        # no new frame for this long looks like a freeze


# ------------------------------------------------------------------ packets
@dataclass
class Pkt:
    t: float          # arrival time (s)
    seq: int          # RTP sequence number (16 bit)
    ts: int           # RTP timestamp (32 bit)
    marker: bool      # last packet of a frame
    pt: int
    size: int         # UDP payload bytes
    res: tuple | None = None   # (width, height) from the RTP/JPEG header of a frame's first packet


def parse_rtp(data: bytes, t: float) -> Pkt | None:
    if len(data) < 12 or data[0] >> 6 != 2:
        return None
    seq, ts = struct.unpack("!HI", data[2:8])
    pt = data[1] & 0x7F
    res = None
    if pt == 26:   # RFC 2435 JPEG header: offset(3) type Q width/8 height/8
        hl = 12 + 4 * (data[0] & 0x0F)
        if data[0] & 0x10 and len(data) >= hl + 4:
            hl += 4 + 4 * struct.unpack("!H", data[hl + 2:hl + 4])[0]
        jh = data[hl:hl + 8]
        if len(jh) == 8 and int.from_bytes(jh[1:4], "big") == 0:
            res = (jh[6] * 8, jh[7] * 8)
    return Pkt(t, seq, ts, bool(data[1] & 0x80), pt, len(data), res)


_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
    b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9),
}


def read_pcap(path: str, port: int) -> list[Pkt]:
    """RTP packets to UDP `port` from a classic pcap file (what tcpdump -w writes)."""
    pkts = []
    with open(path, "rb") as f:
        hdr = f.read(24)
        if hdr[:4] == b"\x0a\x0d\x0d\x0a":
            raise SystemExit("This is a pcapng file. Convert it: editcap -F pcap in.pcapng out.pcap")
        if hdr[:4] not in _PCAP_MAGIC:
            raise SystemExit(f"{path}: not a pcap file")
        endian, frac = _PCAP_MAGIC[hdr[:4]]
        linktype = struct.unpack(endian + "I", hdr[20:24])[0] & 0x0FFFFFFF
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                break
            sec, sub, incl, _orig = struct.unpack(endian + "IIII", rec)
            data = f.read(incl)
            if len(data) < incl:
                break
            payload = _udp_payload(data, linktype, port)
            if payload is not None:
                p = parse_rtp(payload, sec + sub / frac)
                if p:
                    pkts.append(p)
    return pkts


def _udp_payload(frame: bytes, linktype: int, port: int) -> bytes | None:
    if linktype == 1:                      # Ethernet (also Wi-Fi in normal mode)
        off, etype = 14, struct.unpack("!H", frame[12:14])[0]
        while etype in (0x8100, 0x88A8):   # VLAN tags
            etype = struct.unpack("!H", frame[off + 2:off + 4])[0]
            off += 4
    elif linktype == 113:                  # Linux "any" (SLL)
        off, etype = 16, struct.unpack("!H", frame[14:16])[0]
    elif linktype == 276:                  # Linux "any" (SLL2)
        off, etype = 20, struct.unpack("!H", frame[0:2])[0]
    elif linktype in (12, 101):            # raw IP
        off, etype = 0, 0x0800
    elif linktype == 0:                    # loopback
        off, etype = 4, 0x0800
    else:
        return None
    if etype != 0x0800 or len(frame) < off + 28:
        return None
    ip = frame[off:]
    if ip[0] >> 4 != 4 or ip[9] != 17:
        return None
    if struct.unpack("!H", ip[6:8])[0] & 0x3FFF:   # IP fragment: skip
        return None
    udp = ip[(ip[0] & 0x0F) * 4:]
    if struct.unpack("!H", udp[2:4])[0] != port:
        return None
    return udp[8:]


def udp_counters() -> dict[str, int]:
    """Linux UDP counters (RcvbufErrors = packets dropped because a socket buffer was full)."""
    try:
        lines = [l.split() for l in Path("/proc/net/snmp").read_text().splitlines() if l.startswith("Udp:")]
        return {k: int(v) for k, v in zip(lines[0][1:], lines[1][1:])}
    except (OSError, IndexError, ValueError):
        return {}


# ------------------------------------------------------------------ frames
@dataclass
class Frame:
    ts: int                     # unwrapped RTP timestamp
    first_t: float
    last_t: float
    seqs: list = field(default_factory=list)
    marker: bool = False
    size: int = 0
    missing: int = 0
    complete: bool = False


def _unwrap(values: list[int], bits: int) -> list[int]:
    out, mod, half, last = [], 1 << bits, 1 << (bits - 1), None
    for v in values:
        if last is None:
            last = v
        else:
            d = (v - last) % mod
            if d >= half:
                d -= mod
            last += d
        out.append(last)
    return out


def build_frames(pkts: list[Pkt]) -> tuple[list[Frame], list[int]]:
    """Group packets into frames by RTP timestamp. Returns (frames, extended seq per packet)."""
    if not pkts:
        return [], []
    seqs = _unwrap([p.seq for p in pkts], 16)
    tss = _unwrap([p.ts for p in pkts], 32)
    frames: dict[int, Frame] = {}
    for p, s, ts in zip(pkts, seqs, tss):
        fr = frames.get(ts)
        if fr is None:
            fr = frames[ts] = Frame(ts, p.t, p.t)
        fr.first_t, fr.last_t = min(fr.first_t, p.t), max(fr.last_t, p.t)
        fr.seqs.append(s)
        fr.marker |= p.marker
        fr.size += p.size
    ordered = sorted(frames.values(), key=lambda f: min(f.seqs))
    prev_last = None
    for fr in ordered:
        lo = min(fr.seqs) if prev_last is None else prev_last + 1
        hi = max(fr.seqs)
        fr.missing = max(0, (hi - lo + 1) - len(set(fr.seqs)))
        fr.complete = fr.marker and fr.missing == 0
        prev_last = hi if prev_last is None else max(prev_last, hi)
    return ordered, seqs


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    v = sorted(values)
    k = (len(v) - 1) * q / 100.0
    i = int(k)
    return v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i)


# ------------------------------------------------------------------ analysis
def analyze(pkts: list[Pkt], outdir: Path, udp_delta: dict | None = None,
            capture_drops: int | None = None) -> str:
    outdir.mkdir(parents=True, exist_ok=True)
    if len(pkts) < 10:
        msg = ("Almost no video packets received on this port. Is the ROV streaming to this laptop's IP "
               "(check ROV_TOPSIDE_IP / --host on the Pi) and is the firewall open (UDP 5000)?")
        (outdir / "summary.txt").write_text(msg + "\n")
        return msg
    pkts = sorted(pkts, key=lambda p: p.t)
    t0 = pkts[0].t
    frames, ext = build_frames(pkts)
    pt = max(set(p.pt for p in pkts), key=[p.pt for p in pkts].count)
    codec = {26: "MJPEG (RTP/JPEG)", 96: "H.264"}.get(pt, f"payload type {pt}")
    duration = pkts[-1].t - t0

    # packet loss over time, in sequence order so late (reordered) packets aren't counted as lost;
    # each gap is placed at the arrival time of the packet right after it
    by_seq = sorted({s: p.t for p, s in zip(pkts, ext)}.items())
    loss_events = [(t_next - t0, s_next - s - 1)
                   for (s, _), (s_next, t_next) in zip(by_seq, by_seq[1:]) if s_next > s + 1]
    reordered, highest = 0, None
    for s in ext:
        if highest is not None and s < highest:
            reordered += 1
        highest = s if highest is None else max(highest, s)
    expected = max(ext) - min(ext) + 1
    received = len(set(ext))

    # per-frame timing
    comp = [f for f in frames if f.complete]
    rows = []
    base_delay = min(f.last_t - f.ts / CLOCK for f in frames)
    prev = None
    for f in frames:
        extra = (f.last_t - f.ts / CLOCK - base_delay) * 1000
        src = (f.ts - prev.ts) / CLOCK * 1000 if prev else None
        arr = (f.last_t - prev.last_t) * 1000 if prev else None
        rows.append(dict(t=f.last_t - t0, src_ms=src, arrival_ms=arr, extra_delay_ms=extra,
                         transfer_ms=(f.last_t - f.first_t) * 1000, size=f.size,
                         packets=len(f.seqs), missing=f.missing, complete=f.complete))
        prev = f
    src_int = [r["src_ms"] for r in rows if r["src_ms"] is not None and r["src_ms"] > 0]
    nominal = pct(src_int, 50) if src_int else 33.3
    extra = [r["extra_delay_ms"] for r in rows]
    # gaps between consecutive *complete* frames = what the pilot sees
    comp_t = sorted(f.last_t for f in comp)
    shown_gaps = [(b - a) * 1000 for a, b in zip(comp_t, comp_t[1:])]
    freezes = [(comp_t[i + 1] - t0, g) for i, g in enumerate(shown_gaps) if g > FREEZE_MS]
    src_hiccups = [(r["t"], r["src_ms"]) for r in rows if r["src_ms"] and r["src_ms"] > 1.5 * nominal]
    spikes = sorted(((r["t"], r["extra_delay_ms"]) for r in rows if r["extra_delay_ms"] > SPIKE_MS),
                    key=lambda x: -x[1])
    sizes = [f.size for f in frames]

    # per-second buckets
    nsec = int(duration) + 1
    kbit = [0.0] * nsec
    lost_s = [0] * nsec
    fps_s = [0] * nsec
    for p in pkts:
        kbit[min(nsec - 1, int(p.t - t0))] += p.size * 8 / 1000
    for t, gap in loss_events:
        lost_s[min(nsec - 1, int(t))] += gap
    for f in comp:
        fps_s[min(nsec - 1, int(f.last_t - t0))] += 1

    # ---- write CSV
    with open(outdir / "frames.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})

    # ---- summary
    L = []
    sizes_seen = [p.res for p in pkts if p.res]
    res_txt = ""
    if sizes_seen:
        common = max(set(sizes_seen), key=sizes_seen.count)
        res_txt = f", {common[0]}x{common[1]}" + (" (resolution changed during the test)" if len(set(sizes_seen)) > 1 else "")
    L.append(f"Stream: {codec}{res_txt}, {duration:.1f} s, {len(pkts)} packets, {len(frames)} frames")
    L.append(f"Camera frame interval: median {nominal:.1f} ms (~{1000 / nominal:.0f} fps), "
             f"p95 {pct(src_int, 95):.1f} ms, max {max(src_int):.1f} ms")
    L.append(f"Frames shown (complete): {len(comp)}/{len(frames)} = {len(comp) / max(duration, 1e-9):.1f} fps average, "
             f"worst second {min(fps_s[:-1] or fps_s)} fps")
    loss_pct = 100.0 * (expected - received) / expected
    L.append(f"Packet loss: {expected - received} of {expected} ({loss_pct:.2f}%) in {len(loss_events)} gaps; "
             f"{sum(1 for f in frames if f.missing)} frames damaged; {reordered} packets arrived out of order")
    L.append(f"Extra delay (relative to the fastest frame): median {pct(extra, 50):.0f} ms, p95 {pct(extra, 95):.0f} ms, "
             f"p99 {pct(extra, 99):.0f} ms, max {max(extra):.0f} ms")
    L.append(f"Delay spikes: {sum(1 for e in extra if e > SPIKE_MS)} frames > {SPIKE_MS:.0f} ms, "
             f"{sum(1 for e in extra if e > BAD_SPIKE_MS)} frames > {BAD_SPIKE_MS:.0f} ms")
    L.append(f"Freezes (no new complete frame for > {FREEZE_MS:.0f} ms): {len(freezes)}"
             + (f", longest {max(g for _, g in freezes):.0f} ms" if freezes else ""))
    L.append(f"Uneven frames from the ROV (> 1.5x normal interval): {len(src_hiccups)}")
    L.append(f"Bitrate: average {sum(kbit) / max(duration, 1e-9) / 1000:.1f} Mbit/s, peak second {max(kbit) / 1000:.1f} Mbit/s; "
             f"frame size median {pct(sizes, 50) / 1000:.0f} kB, max {max(sizes) / 1000:.0f} kB")
    L.append(f"Time to receive one frame (first to last packet): median {pct([r['transfer_ms'] for r in rows], 50):.1f} ms, "
             f"p95 {pct([r['transfer_ms'] for r in rows], 95):.1f} ms")
    if udp_delta:
        L.append(f"Laptop UDP buffer overflows during the test: {udp_delta.get('RcvbufErrors', 0)}")
    if capture_drops:
        L.append(f"Note: tcpdump itself dropped {capture_drops} packets; some 'loss' above may be the capture, not the network.")
    if spikes:
        L.append("Worst delay spikes (time into test, extra delay): "
                 + ", ".join(f"{t:.1f}s {d:.0f}ms" for t, d in spikes[:6]))
    if freezes:
        L.append("Freezes: " + ", ".join(f"{t:.1f}s {g:.0f}ms" for t, g in sorted(freezes, key=lambda x: -x[1])[:6]))

    # ---- verdict
    V = []
    if loss_pct > 0.5 or sum(1 for f in frames if f.missing) > 0.02 * len(frames):
        V.append(f"- Packets are being lost ({loss_pct:.2f}%). With MJPEG each lost packet throws away a whole frame, "
                 "which is the stutter. The link can't carry this bitrate reliably: try the tether, turn off Wi-Fi "
                 "power saving (pi_check.sh shows it), or lower the load (720p, or camera-h264).")
    if len(src_hiccups) >= max(3, 0.01 * len(frames)):
        V.append(f"- The ROV itself sends frames unevenly ({len(src_hiccups)} late frames before the network). "
                 "Check the camera (exposure_dynamic_framerate=0, power_line_frequency=2), USB disconnects, "
                 "and the Pi's CPU with pi_check.sh.")
    if sum(1 for e in extra if e > BAD_SPIKE_MS) >= max(3, 0.005 * len(frames)):
        V.append("- Frames arrive late in bursts (delay spikes > 100 ms) without being lost: classic Wi-Fi behavior "
                 "(power saving, retries, interference, a busy network). Turn off power saving on both ends; "
                 "the tether should remove most of this.")
    if udp_delta and udp_delta.get("RcvbufErrors", 0) > 0:
        V.append("- The laptop dropped packets because the interface didn't read them fast enough "
                 "(socket buffer full). Run: sudo sysctl -w net.core.rmem_max=8388608, and check the laptop's CPU "
                 "(YOLO on the CPU can starve the video thread).")
    if not V:
        V.append("- The stream arriving at the laptop looks clean. If you still see stutter, it's in the interface: "
                 "try with YOLO off, and watch the laptop's CPU (top) while it runs.")
    L.append("")
    L.append("What it means:")
    L += V
    summary = "\n".join(L)
    (outdir / "summary.txt").write_text(summary + "\n")

    # ---- chart
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(4, 1, figsize=(12, 9), sharex=True)
        ts = [r["t"] for r in rows]
        ax[0].plot(ts, extra, lw=0.8)
        ax[0].axhline(BAD_SPIKE_MS, color="tab:red", lw=0.8, ls="--")
        ax[0].set_ylabel("extra delay\n(ms)")
        ax[0].set_title("ROV video stream: where the stutter comes from")
        ax[1].plot([r["t"] for r in rows if r["src_ms"]], [r["src_ms"] for r in rows if r["src_ms"]], lw=0.8,
                   color="tab:orange")
        ax[1].set_ylabel("frame interval\nfrom ROV (ms)")
        n = nsec - 1 if duration - int(duration) < 0.5 and nsec > 1 else nsec   # drop a partial last second
        secs = list(range(n))
        kbit, lost_s, fps_s = kbit[:n], lost_s[:n], fps_s[:n]
        ax[2].bar(secs, [k / 1000 for k in kbit], width=0.9, color="tab:green")
        ax[2].set_ylabel("Mbit/s")
        ax[3].bar(secs, lost_s, width=0.9, color="tab:red", label="lost packets")
        ax3b = ax[3].twinx()
        ax3b.plot(secs, fps_s, color="tab:blue", lw=1, label="fps shown")
        ax[3].set_ylabel("lost packets / s")
        ax3b.set_ylabel("fps shown")
        ax[3].set_xlabel("seconds into test")
        fig.tight_layout()
        fig.savefig(outdir / "timeline.png", dpi=120)
        plt.close(fig)
    except ImportError:
        pass
    return summary


# ------------------------------------------------------------------ modes
def default_outdir() -> Path:
    return Path.cwd() / time.strftime("video_check_%Y%m%d_%H%M%S")


def cmd_analyze(args) -> int:
    out = Path(args.out) if args.out else Path(args.pcap).with_suffix("")
    print(analyze(read_pcap(args.pcap, args.port), out))
    print(f"\nFiles: {out}/")
    return 0


def cmd_capture(args) -> int:
    if not any(Path(d, "tcpdump").exists() for d in os.environ.get("PATH", "").split(":") + ["/usr/sbin"]):
        print("tcpdump is not installed: sudo dnf install -y tcpdump")
        return 1
    out = Path(args.out) if args.out else default_outdir()
    out.mkdir(parents=True, exist_ok=True)
    pcap = out / "capture.pcap"
    before = udp_counters()
    print(f"Capturing UDP {args.port} on '{args.iface}' for {args.seconds} s (keep the interface running)...")
    cmd = ["sudo", "timeout", str(args.seconds), "tcpdump", "-i", args.iface, "-n", "-B", "16384",
           "-Z", getpass.getuser(), "-w", str(pcap), "udp", "port", str(args.port)]
    proc = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    after = udp_counters()
    drops = None
    for line in proc.stderr.splitlines():
        if "dropped by kernel" in line:
            try:
                drops = int(line.split()[0])
            except ValueError:
                pass
    if not pcap.exists():
        print(proc.stderr)
        return 1
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    print(analyze(read_pcap(str(pcap), args.port), out, delta, drops))
    print(f"\nFiles: {out}/  (summary.txt, frames.csv, timeline.png, capture.pcap)")
    return 0


def cmd_live(args) -> int:
    out = Path(args.out) if args.out else default_outdir()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    try:
        sock.bind(("", args.port))
    except OSError as e:
        print(f"Can't listen on UDP {args.port} ({e}). Close the vision interface, or use: video_check.py capture")
        return 1
    sock.settimeout(0.5)
    before = udp_counters()
    pkts: list[Pkt] = []
    start = time.time()
    next_print, sec_pkts = start + 1, []
    print(f"Listening on UDP {args.port} for {args.seconds} s...")
    print("  time   Mbit/s  frames  lost")
    while time.time() - start < args.seconds:
        try:
            data, _ = sock.recvfrom(65536)
            p = parse_rtp(data, time.time())
            if p:
                pkts.append(p)
                sec_pkts.append(p)
        except socket.timeout:
            pass
        if time.time() >= next_print:
            if sec_pkts:
                s = _unwrap([p.seq for p in sec_pkts], 16)
                lost = (max(s) - min(s) + 1) - len(set(s))
                print(f"  {next_print - start:4.0f}s  {sum(p.size for p in sec_pkts) * 8 / 1e6:6.1f}  "
                      f"{sum(1 for p in sec_pkts if p.marker):6d}  {lost:4d}")
            else:
                print(f"  {next_print - start:4.0f}s  (no packets)")
            sec_pkts, next_print = [], next_print + 1
    after = udp_counters()
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    print()
    print(analyze(pkts, out, delta))
    print(f"\nFiles: {out}/")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Analyze the ROV video stream for lag spikes and stutter.")
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("capture", help="tcpdump the stream while the interface runs, then analyze")
    c.add_argument("--seconds", type=int, default=60)
    c.add_argument("--iface", default="any", help="network interface (default: any)")
    lv = sub.add_parser("live", help="listen on the port yourself (interface must be closed)")
    lv.add_argument("--seconds", type=int, default=30)
    an = sub.add_parser("analyze", help="analyze a pcap file")
    an.add_argument("pcap")
    for s in (c, lv, an):
        s.add_argument("--port", type=int, default=5000)
        s.add_argument("--out", help="output folder")
    args = ap.parse_args(argv)
    return {"capture": cmd_capture, "live": cmd_live, "analyze": cmd_analyze}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
