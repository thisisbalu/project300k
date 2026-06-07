#!/usr/bin/env python3
"""
Ford Mode 22 PID validation script — run on Mac against a live car.

Queries each candidate Ford PID address and reports raw bytes + parsed value.
Use results to confirm which addresses are correct before updating obd_commands.py.

Requirements:
  - Car engine must be ON
  - OBDLink MX+ paired to this Mac via Bluetooth
  - pi/venv activated (python-obd installed)

Single run:
  python validate_ford_pids.py /dev/tty.OBDLinkMX66328

Continuous loop (logs to file — use for drive sessions):
  python validate_ford_pids.py /dev/tty.OBDLinkMX66328 --loop
  python validate_ford_pids.py /dev/tty.OBDLinkMX66328 --loop 5   # 5s interval

Scan mode (probe candidate addresses for unknown PIDs):
  python validate_ford_pids.py /dev/tty.OBDLinkMX66328 --scan

Find your port:
  ls /dev/tty.* | grep -i obd
"""
from __future__ import annotations

import sys
import time
import datetime
import obd
from obd import OBDCommand, ECU


PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/tty.OBDLinkMX66328"

LOOP = "--loop" in sys.argv
SCAN = "--scan" in sys.argv
INTERVAL = 3
for i, arg in enumerate(sys.argv):
    if arg == "--loop" and i + 1 < len(sys.argv):
        try:
            INTERVAL = int(sys.argv[i + 1])
        except ValueError:
            pass


def s8(b: int) -> int:
    return b - 256 if b > 127 else b


def s16(hi: int, lo: int) -> int:
    v = (hi << 8) | lo
    return v - 65536 if v > 32767 else v


def raw_decoder(messages):
    if not messages:
        return None
    try:
        frame = messages[0].frames[0]
        data = bytes(frame.data)
        if data:
            return data
        # ELM327 returned "NO DATA" — ECU did not respond to this address
        raw_str = getattr(frame, 'raw', '')
        if raw_str == 'NO DATA':
            return None
        # Try parsing raw string as fallback (e.g. for NRC bytes)
        parts = raw_str.split()
        if len(parts) > 1:
            return bytes(int(x, 16) for x in parts[1:] if len(x) == 2)
        return None
    except (AttributeError, IndexError, ValueError):
        return None


def assemble_isotp(frames) -> bytes:
    """Assemble ISO-TP payload by stripping per-frame headers from all CAN frames.

    Frame types:
      Single Frame (0x0n):      [len_nibble, payload...]  — strip 1 byte
      First Frame (0x1n 0xnn):  [0x1n, len_lo, payload...] — strip 2 bytes
      Consecutive Frame (0x2n): [seq, payload...]          — strip 1 byte
    """
    payload = bytearray()
    for frame in frames:
        data = bytes(frame.data)
        if not data:
            continue
        ftype = (data[0] & 0xF0) >> 4
        if ftype == 0:
            n = data[0] & 0x0F
            payload.extend(data[1:1 + n])
        elif ftype == 1:
            payload.extend(data[2:])
        elif ftype == 2:
            payload.extend(data[1:])
    return bytes(payload)


def mode06_decoder(messages):
    """Assemble a multi-frame Mode 06 response into one bytes object.

    Returns the full payload starting with 0x46 (positive Mode 06 service byte),
    or None on any error so the caller stores NULL rather than crashing.
    """
    if not messages:
        return None
    try:
        frames = messages[0].frames
        if not frames:
            return None
        payload = assemble_isotp(frames)
        if len(payload) < 2 or payload[0] != 0x46:
            return None
        return payload
    except (AttributeError, IndexError):
        return None


# CAN frame structure confirmed: [len] [62] [PID_H] [PID_L] [data_A] [data_B ...]
# Data starts at index 4.

ENGINE_PIDS = [
    # Confirmed working
    ("oil_pressure_kpa",   b"220415", 6, lambda d: (d[4] * 256) + d[5],                         "kPa"),
    ("knock_retard_deg",   b"2203EC", 6, lambda d: round(s8(d[4]) / 2 + d[5] / 512, 3),         "°"),
    ("boost_desired_psi",  b"220461", 6, lambda d: round(((d[4] * 256) + d[5]) * 0.0145, 2),    "psi"),
    ("boost_actual_psi",   b"220462", 6, lambda d: round(((d[4] * 256) + d[5]) * 0.0145, 2),    "psi"),
    ("cac_temp_c",         b"2203CA", 5, lambda d: s8(d[4]),                                     "°C"),
    ("wastegate_pct",      b"2203E3", 6, lambda d: round(((d[4] * 256) + d[5]) / 100, 2),       "%"),
    ("vct_intake_deg",     b"220303", 6, lambda d: round(s16(d[4], d[5]) / 16, 2),              "°"),

    # Formula confirmed: scale /256, BASE=26858 from warm city driving (exhaust cam parked ≈0°)
    ("vct_exhaust_deg",    b"220304", 6, lambda d: round(((d[4]*256)+d[5]-26858)/256, 2),       "°"),

    # Mode 06 misfire counters — confirmed responding 2026-06-06.
    # Raw first CAN frame layout: [0x10, len, 0x46, TID, OBDMID, SDTID, val_hi, val_lo, ...]
    # d[6]/d[7] = test value of first record (OBDMID=0x0B, SDTID=0x24 = unsigned count × 1)
    ("misfire_acc_cyl1",   b"06A2", 8, lambda d: (d[6] * 256) + d[7],                           "counts"),
    ("misfire_acc_cyl2",   b"06A3", 8, lambda d: (d[6] * 256) + d[7],                           "counts"),
    ("misfire_acc_cyl3",   b"06A4", 8, lambda d: (d[6] * 256) + d[7],                           "counts"),
    ("misfire_acc_cyl4",   b"06A5", 8, lambda d: (d[6] * 256) + d[7],                           "counts"),

    # Wrong addresses — need Portmon capture
    ("fuel_rail_pressure", b"2202FD", 6, lambda d: (d[4] * 256) + d[5],                         "kPa"),
    ("oil_temp_c",         b"2204FE", 5, lambda d: d[4] - 40,                                   "°C"),
    ("misfire_acc_cyl1",   b"22160E", 6, lambda d: (d[4] * 256) + d[5],                         ""),
    ("misfire_acc_cyl2",   b"22160F", 6, lambda d: (d[4] * 256) + d[5],                         ""),
    ("misfire_acc_cyl3",   b"221610", 6, lambda d: (d[4] * 256) + d[5],                         ""),
    ("misfire_acc_cyl4",   b"221611", 6, lambda d: (d[4] * 256) + d[5],                         ""),
]

TRANS_PIDS = [
    # trans_temp confirmed. trans_gear and tcc_ratio respond but encoding unclear.
    ("trans_temp_c",       b"221E1C", 6, lambda d: round(s16(d[4], d[5]) / 16, 1),              "°C"),
    ("trans_gear",         b"221E12", 5, lambda d: d[4],                                         "raw"),
    ("tcc_ratio",          b"221E1F", 5, lambda d: round(d[4] / 255, 3),                         "ratio"),
]

# Misfire accumulator monitors — confirmed responding via --scan (2026-06-06).
# TID A2–A5 = cylinders 1–4. Each response is 37 bytes assembled from 6 CAN frames.
# Payload: [0x46, TID, then repeated 8-byte records: OBDMID, SDTID, val_hi, val_lo, min_hi, min_lo, max_hi, max_lo]
MODE06_PIDS = [
    ("misfire_acc_cyl1", b"06A2", "cyl1"),
    ("misfire_acc_cyl2", b"06A3", "cyl2"),
    ("misfire_acc_cyl3", b"06A4", "cyl3"),
    ("misfire_acc_cyl4", b"06A5", "cyl4"),
]

# Candidate addresses to scan — used with --scan flag.
# Any address returning a positive response (starts with 62) is interesting.
SCAN_PIDS = [
    # Mode 01 standard PID — fuel rail pressure on GDI engines
    b"0123",

    # Fuel rail pressure candidates (2202xx neighbourhood)
    b"220234", b"220238", b"220240",
    b"2202E0", b"2202F0", b"2202F2", b"2202FA", b"2202FC",

    # Oil temp candidates (2204xx neighbourhood)
    b"22049C", b"2204A0", b"2204A4",
    b"2204C2", b"2204D0", b"2204E0", b"2204F2", b"2204FA",

    # Misfire accumulator candidates — block A (2210xx)
    b"22100E", b"22100F", b"221010", b"221011",

    # Misfire accumulator candidates — block B (221Axx)
    b"221A0E", b"221A0F", b"221A10", b"221A11",

    # Misfire original addresses — retesting for comparison
    b"22160E", b"22160F", b"221610", b"221611",

    # Mode 06 misfire TIDs — response byte[1] == 0x46 (not 0x62)
    # TID A2–A5: cylinder-specific misfire monitors (Ford 2.0L EcoBoost candidate range)
    b"06A2", b"06A3", b"06A4", b"06A5",
    # TID 85: generic misfire IUMPR monitor
    b"0685",
]


def run_scan(conn) -> None:
    """Test every address in SCAN_PIDS and print raw bytes for any that respond."""
    print("\nSCAN MODE — looking for responding addresses")
    print("  " + "─" * 70)
    print(f"  {'Address':<12} {'Raw bytes':<35} Note")
    print("  " + "─" * 70)

    hits = []
    for addr in SCAN_PIDS:
        cmd = OBDCommand(addr.decode(), addr.decode(), addr, 8, raw_decoder, ECU.ALL, fast=False)
        resp = conn.query(cmd, force=True)

        addr_str = addr.decode()
        if resp.is_null() or resp.value is None:
            print(f"  {addr_str:<12} NO DATA")
            continue

        raw: bytes = resp.value
        if not raw:
            print(f"  {addr_str:<12} NO DATA")
            continue

        raw_hex = raw.hex(" ").upper()

        # ISO-TP First Frame (raw[0] == 0x10) shifts service ID to raw[2].
        # Single frame: [len, svc_id, ...]. First Frame: [0x10, len_lo, svc_id, ...]
        svc_idx = 2 if (len(raw) > 0 and raw[0] == 0x10) else 1

        if len(raw) > svc_idx and raw[svc_idx] in (0x62, 0x41, 0x46):
            note = "RESPOND ← address confirmed"
            hits.append(addr_str)
        elif len(raw) >= svc_idx + 3 and raw[svc_idx] == 0x7F:
            nrc = raw[svc_idx + 2] if len(raw) > svc_idx + 2 else 0
            if nrc == 0x22:
                # NRC22 = address exists, ECU conditions not met (e.g. engine not warm)
                note = "NRC22 ← address confirmed (conditions not met)"
                hits.append(addr_str)
            elif nrc == 0x31:
                note = "NRC31 wrong address"
            else:
                note = f"NRC {nrc:02X}"
        else:
            note = "unexpected response"

        print(f"  {addr_str:<12} {raw_hex:<35} {note}")

    print("  " + "─" * 70)
    if hits:
        print(f"\n  Addresses that responded: {', '.join(hits)}")
    else:
        print("\n  No addresses responded.")


def run_once(conn, log=None) -> None:
    col_header = f"  {'Parameter':<24} {'Address':<10} {'Raw (hex)':<30} Value"
    divider = "  " + "─" * 78

    lines = []
    lines.append("ENGINE (PCM — header 7E0)")
    lines.append(divider)
    lines.append(col_header)
    lines.append(divider)
    for pid in ENGINE_PIDS:
        lines.append(capture_pid(conn, *pid))

    lines.append(f"\nTRANSMISSION (TCM — header 7E1)")
    lines.append(divider)
    lines.append(col_header)
    lines.append(divider)
    for pid in TRANS_PIDS:
        lines.append(capture_pid(conn, *pid))

    lines.append(f"\nMISFIRE COUNTERS (Mode 06 — PCM header 7E0)")
    lines.append(divider)
    lines.append(col_header)
    lines.append(divider)
    for label, addr, cylinder in MODE06_PIDS:
        lines.append(capture_mode06(conn, label, addr, cylinder))

    output = "\n".join(lines)
    print(output)
    if log:
        log.write(output + "\n")
        log.flush()


def capture_pid(conn, label: str, addr: bytes, exp_bytes: int, formula, unit: str) -> str:
    cmd = OBDCommand(label, label, addr, exp_bytes, raw_decoder, ECU.ALL, fast=False)
    resp = conn.query(cmd, force=True)

    addr_str = addr.decode()
    if resp.is_null() or resp.value is None:
        return f"  {label:<24} {addr_str:<10} NO DATA"

    raw: bytes = resp.value
    raw_hex = raw.hex(" ").upper()

    if len(raw) >= 4 and raw[1] == 0x7F and raw[2] == 0x22:
        nrc = raw[3]
        nrc_label = {
            0x31: "NRC31 wrong address",
            0x22: "NRC22 conditions not met",
            0x12: "NRC12 sub-function not supported",
        }.get(nrc, f"NRC {nrc:02X}")
        return f"  {label:<24} {addr_str:<10} {raw_hex:<30} {nrc_label}"

    try:
        value = formula(list(raw))
        return f"  {label:<24} {addr_str:<10} {raw_hex:<30} {value} {unit}"
    except (IndexError, ZeroDivisionError) as exc:
        return f"  {label:<24} {addr_str:<10} {raw_hex:<30} formula err: {exc} (len={len(raw)})"


def capture_mode06(conn, label: str, addr: bytes, cylinder: str) -> str:
    """Query a Mode 06 TID and display all assembled test result records."""
    cmd = OBDCommand(label, label, addr, 40, mode06_decoder, ECU.ALL, fast=False)
    resp = conn.query(cmd, force=True)

    addr_str = addr.decode()
    if resp.is_null() or resp.value is None:
        return f"  {label:<24} {addr_str:<10} NO DATA"

    payload: bytes = resp.value
    raw_hex = payload.hex(" ").upper()

    # Parse 8-byte test result records: [OBDMID, SDTID, val_hi, val_lo, min_hi, min_lo, max_hi, max_lo]
    # payload[0]=0x46 (svc), payload[1]=TID, records start at offset 2
    records = []
    offset = 2
    while offset + 8 <= len(payload):
        obdmid = payload[offset]
        val   = (payload[offset + 2] << 8) | payload[offset + 3]
        min_v = (payload[offset + 4] << 8) | payload[offset + 5]
        max_v = (payload[offset + 6] << 8) | payload[offset + 7]
        records.append(f"[MID=0x{obdmid:02X} val={val} min={min_v} max={max_v}]")
        offset += 8

    if records:
        parsed = "  ".join(records)
        return f"  {label:<24} {addr_str:<10} {cylinder}: {parsed}"
    # Fallback — record size may not be 8; show raw hex for manual inspection
    return f"  {label:<24} {addr_str:<10} {cylinder}: raw={raw_hex}  ({len(payload)}B)"


def main() -> None:
    print(f"Connecting to {PORT} ...")
    conn = obd.OBD(PORT, fast=False, timeout=30)

    if not conn.is_connected():
        print("ERROR: could not connect.")
        print("  - Is the car engine ON?")
        print("  - Is OBDLink MX+ paired to this Mac via Bluetooth?")
        print(f"  - Is {PORT} the right port?  Run: ls /dev/tty.* | grep -i obd")
        sys.exit(1)

    print(f"Connected — {conn.protocol_name()}")

    if SCAN:
        run_scan(conn)
        conn.close()
        return

    if not LOOP:
        print()
        run_once(conn)
        conn.close()
        return

    log_path = f"pid_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    print(f"Loop mode — interval {INTERVAL}s — logging to {log_path}")
    print("Press Ctrl+C to stop.\n")

    with open(log_path, "w") as log:
        try:
            run_count = 0
            while True:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                header = f"\n{'─'*20} {ts} (run {run_count + 1}) {'─'*20}"
                print(header)
                log.write(header + "\n")
                run_once(conn, log)
                run_count += 1
                time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print(f"\nStopped. {run_count} runs logged to {log_path}")

    conn.close()


if __name__ == "__main__":
    main()
