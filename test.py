"""
MRL Race Telemetry – UDP Test Sender
=====================================
Simuliert F1 25 UDP-Pakete (Packet 2, 4, 6, 7) damit das Overlay
ohne laufendes Spiel getestet werden kann.

Starten: python test.py
Dann im Browser: http://127.0.0.1:5100
"""

import socket
import struct
import time
import math
import random

UDP_IP   = "127.0.0.1"
UDP_PORT = 20777

# ── Testdaten ──────────────────────────────────────────────────────────────────

DRIVERS = [
    {"name": "Kers",              "team_id": 4,  "compound": 16},  # Aston / Soft
    {"name": "typisch_AyBee",     "team_id": 3,  "compound": 17},  # McLaren / Med
    {"name": "GFL_Der_Profi",     "team_id": 3,  "compound": 17},  # McLaren / Med
    {"name": "Dsilvaa28",         "team_id": 9,  "compound": 18},  # Alpine / Hard
    {"name": "Knockout",          "team_id": 1,  "compound": 16},  # Red Bull / Soft
    {"name": "Ryan",              "team_id": 6,  "compound": 16},  # Ferrari / Soft
    {"name": "Reuti",             "team_id": 5,  "compound": 17},  # RB / Med
    {"name": "Simi",              "team_id": 2,  "compound": 18},  # Haas / Hard
    {"name": "Noah",              "team_id": 6,  "compound": 17},  # Ferrari / Med
    {"name": "Aaron",             "team_id": 0,  "compound": 18},  # Mercedes / Hard
    {"name": "Lukas_MRL",         "team_id": 1,  "compound": 16},  # Red Bull / Soft
    {"name": "FastFabi",          "team_id": 7,  "compound": 17},  # Williams / Med
    {"name": "xX_Speedy_Xx",      "team_id": 9,  "compound": 16},  # Alpine / Soft
    {"name": "DriveOrDie99",      "team_id": 8,  "compound": 18},  # Williams / Hard
    {"name": "MaxThrottle",       "team_id": 0,  "compound": 17},  # Mercedes / Med
    {"name": "PitLane_Hero",      "team_id": 4,  "compound": 16},  # Aston / Soft
    {"name": "SlipstreamKing",    "team_id": 2,  "compound": 17},  # Haas / Med
    {"name": "NightRacer",        "team_id": 10, "compound": 18},  # Sauber / Hard
    {"name": "TurboTimo",         "team_id": 5,  "compound": 16},  # RB / Soft
    {"name": "GridGhost",         "team_id": 10, "compound": 17},  # Sauber / Med
]

HEADER_FORMAT = '<HBBBBBQfIIBB'
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

def make_header(packet_id: int) -> bytes:
    return struct.pack(
        HEADER_FORMAT,
        2025, 1, 0, 0, 1,
        packet_id,
        12345678,
        0.0, 0, 0, 0, 255,
    )

# ── Packet 4: Participants ─────────────────────────────────────────────────────

PARTICIPANT_SIZE = 57

def make_participants() -> bytes:
    data = bytes([len(DRIVERS)])
    for i in range(22):
        pd = bytearray(PARTICIPANT_SIZE)
        if i < len(DRIVERS):
            d = DRIVERS[i]
            pd[3] = d["team_id"]
            name = d["name"].encode("utf-8")[:47]
            pd[7:7+len(name)] = name
        data += bytes(pd)
    return make_header(4) + data

# ── Packet 2: Lap Data ─────────────────────────────────────────────────────────

TOTAL_LAPS = 30

QUALI_DURATION = 15 * 60  # 15 Minuten Q

def make_session(t: float, mode: str = "r") -> bytes:
    sd = bytearray(200)
    sd[0] = 0   # weather: clear
    sd[1] = 38  # trackTemperature
    sd[2] = 22  # airTemperature
    if mode == "r":
        sd[3] = TOTAL_LAPS
        sd[6] = 10  # sessionType: Rennen
        import struct
        struct.pack_into('<H', sd, 10, 0)  # sessionTimeLeft: 0 (nicht relevant)
    else:
        sd[3] = 0   # keine Runden im Quali
        sd[6] = 6   # sessionType: Q2
        time_left = max(0, int(QUALI_DURATION - t))
        import struct
        struct.pack_into('<H', sd, 10, time_left)
    return make_header(1) + bytes(sd)

LAP_DATA_SIZE = 57

# Stabile Startreihenfolge, tauscht alle 15s ein Paar
_positions = list(range(1, len(DRIVERS) + 1))
_last_swap = -1

def make_lap_data(t: float) -> bytes:
    global _positions, _last_swap

    current_lap = min(int(t / 90) + 1, TOTAL_LAPS)

    cycle = int(t / 15)
    if cycle != _last_swap and cycle > 0:
        a = random.randint(0, len(DRIVERS) - 2)
        _positions[a], _positions[a+1] = _positions[a+1], _positions[a]
        _last_swap = cycle

    data = b""  # PacketLapData: kein numActiveCars, direkt Array
    for i in range(22):
        ld = bytearray(LAP_DATA_SIZE)
        if i < len(DRIVERS):
            pos       = _positions[i]
            lap_ms    = int((90 + random.uniform(-2, 2)) * 1000)
            s1_ms     = int((28 + random.uniform(-0.8, 0.8)) * 1000)
            s2_ms     = int((32 + random.uniform(-0.8, 0.8)) * 1000)
            gap_front  = 0 if pos == 1 else int(random.uniform(0.2, 6.0) * 1000)
            gap_leader = 0 if pos == 1 else int((pos - 1) * random.uniform(2.0, 5.0) * 1000)

            struct.pack_into('<I', ld, 0,  lap_ms)
            struct.pack_into('<H', ld, 8,  s1_ms % 60000)
            ld[10] = s1_ms // 60000
            struct.pack_into('<H', ld, 11, s2_ms % 60000)
            ld[13] = s2_ms // 60000
            struct.pack_into('<H', ld, 14, min(gap_front,  65535))   # deltaToCarInFrontMSPart
            ld[16] = 0                                                  # deltaToCarInFrontMinutesPart
            struct.pack_into('<H', ld, 17, min(gap_leader, 65535))    # deltaToRaceLeaderMSPart
            ld[19] = 0                                                  # deltaToRaceLeaderMinutesPart
            ld[32] = pos
            ld[33] = current_lap   # currentLapNum — steigt mit der Zeit
            ld[34] = 2 if i == 5 else 0  # pitStatus
            ld[45] = 2  # resultStatus: 2 = active

        data += bytes(ld)
    return make_header(2) + data

# ── Packet 7: Car Status ───────────────────────────────────────────────────────

CAR_STATUS_SIZE = 55
ERS_MAX = 4_000_000.0

def make_car_status(t: float) -> bytes:
    data = b""  # PacketCarStatusData: kein numActiveCars, direkt Array
    for i in range(22):
        cs = bytearray(CAR_STATUS_SIZE)
        if i < len(DRIVERS):
            cs[22] = 1
            cs[26] = DRIVERS[i]["compound"]
            cs[27] = 5 + i
            ers = ERS_MAX * (0.5 + 0.45 * math.sin(t * 0.25 + i * 0.7))
            struct.pack_into('<f', cs, 37, ers)
        data += bytes(cs)
    return make_header(7) + data

# ── Packet 6: Car Telemetry ────────────────────────────────────────────────────

CAR_TELEM_SIZE = 60

def make_car_telemetry(t: float) -> bytes:
    data = b""
    for i in range(22):
        ct = bytearray(CAR_TELEM_SIZE)
        if i < len(DRIVERS):
            drs_open = (i < 4) and (int(t / 6) % 2 == 0)
            ct[18] = 1 if drs_open else 0  # drs: 0=off, 1=on (Offset 18)
        data += bytes(ct)
    return make_header(6) + data

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  MRL Telemetry Test Sender")
    print(f"  → {UDP_IP}:{UDP_PORT}  |  {len(DRIVERS)} Fahrer")
    print("=" * 50)
    print("  [R] Rennen   [Q] Qualifying")
    print("=" * 50)
    while True:
        choice = input("  Modus wählen (R/Q): ").strip().lower()
        if choice in ("r", "q"):
            break
    SESSION_MODE = choice
    mode_name = "RENNEN" if SESSION_MODE == "r" else "QUALIFYING (Q2, 15min)"
    print(f"  → Starte {mode_name}")
    print("  Strg+C zum Beenden")
    print("=" * 50)

    # Test ob Socket funktioniert
    try:
        sock.sendto(b"ping", (UDP_IP, UDP_PORT))
        print(f"[OK] Socket sendet auf {UDP_IP}:{UDP_PORT}")
    except Exception as e:
        print(f"[FEHLER] Socket-Problem: {e}")
        return

    sock.sendto(make_participants(), (UDP_IP, UDP_PORT))
    sock.sendto(make_session(0, SESSION_MODE), (UDP_IP, UDP_PORT))
    print("[P4] Participants gesendet")
    print("[P1] Session gesendet")
    time.sleep(0.3)

    tick  = 0
    start = time.time()

    while True:
        t = time.time() - start

        if tick % 100 == 0:
            sock.sendto(make_participants(), (UDP_IP, UDP_PORT))
        if tick % 20 == 0:
            sock.sendto(make_session(t, SESSION_MODE), (UDP_IP, UDP_PORT))

        sock.sendto(make_lap_data(t),      (UDP_IP, UDP_PORT))
        sock.sendto(make_car_status(t),    (UDP_IP, UDP_PORT))
        sock.sendto(make_car_telemetry(t), (UDP_IP, UDP_PORT))

        if tick % 20 == 0:
            if SESSION_MODE == "r":
                lap = min(int(t / 90) + 1, TOTAL_LAPS)
                print(f"[{t:6.1f}s] Tick {tick:4d} | Runde {lap}/{TOTAL_LAPS} | {len(DRIVERS)} Fahrer")
            else:
                tl = max(0, int(QUALI_DURATION - t))
                print(f"[{t:6.1f}s] Tick {tick:4d} | Q2 verbleibend {tl//60}:{tl%60:02d} | {len(DRIVERS)} Fahrer")

        tick += 1
        time.sleep(0.05)  # 20 Hz

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
