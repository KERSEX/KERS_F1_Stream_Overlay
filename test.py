"""
MRL Race Telemetry – UDP Test Sender v3
========================================
Simuliert F1 25 ODER F1 26 UDP-Pakete und schickt sie an den Overlay-Listener
(127.0.0.1:20777). Über GAME_FORMAT unten wählst du das Spiel-Format:
  2025 -> F1 25 Pakete                       (passend zu mainf125.py)
  2026 -> F1 26 / 2026-Season-Pack Pakete    (passend zu main.py)
Alle Offsets und Struct-Größen werden passend zum gewählten Format gesetzt.

Modi:
  R/RSC/RVSC  Rennen (ohne / mit Safety Car / mit Virtual Safety Car)
  Q1/Q2/Q3    Qualifying

Im Rennen ist der Safety-Car-Status ab Sekunde 5 dauerhaft aktiv, damit du den
Glow-Effekt im Overlay live sehen kannst.
"""

import socket, struct, time, math, random

UDP_IP   = "127.0.0.1"
UDP_PORT = 20777

# ── Spiel-Format ──────────────────────────────────────────────────────────────
# 2025 -> F1 25 Pakete (passend zu mainf125.py)
# 2026 -> F1 26 / 2026-Season-Pack Pakete (passend zu main.py)
GAME_FORMAT = 2026
IS_2026     = GAME_FORMAT == 2026

# Anzahl Auto-Slots in allen Arrays (F1 26 = 24, F1 25 = 22)
NUM_CARS = 24 if IS_2026 else 22

# Participant-Layout: in F1 26 sind Driver-/Network-/Team-Id uint16 -> Struct 57->60,
# Name-Offset 7->10, Team-Offset 3->5 (uint16!), Startnummer-Offset 5->8.
PARTICIPANT_SIZE = 60 if IS_2026 else 57
P4_TEAM_OFFSET   = 5  if IS_2026 else 3
P4_NUMBER_OFFSET = 8  if IS_2026 else 5
P4_NAME_OFFSET   = 10 if IS_2026 else 7

# Car Status: F1 26 hat ein neues Feld -> Struct 55->59 (Offsets 26/27/37 bleiben).
CAR_STATUS_SIZE = 59 if IS_2026 else 55
# Car Telemetry: engineTemperature uint16->uint8 -> Struct 60->59 (DRS bleibt +18).
CAR_TELEM_SIZE  = 59 if IS_2026 else 60
# Car Telemetry 2 (NUR 2026-Pack, pid 16): 10 Bytes/Auto. Enthält den Overtake Mode
# (DRS-Ersatz): m_overtakeAvailable @+4, m_overtakeActive @+5, m_2026Regulations @+8.
CAR_TELEM2_SIZE = 10

# Formula-Id (13 = "F1 26", 0 = F1) und Session-Type-Id fürs Rennen
# (F1 26 = 15, F1 25 = 10; Quali Q1/Q2/Q3 = 5/6/7 in beiden Formaten).
FORMULA_ID      = 13 if IS_2026 else 0
RACE_SESSION_ID = 15 if IS_2026 else 10

# ── Fahrer ────────────────────────────────────────────────────────────────────
# team_id passend zu main.py TEAM_NAMES:
#   0 Mercedes | 1 Ferrari | 2 Red Bull | 3 Williams | 4 Aston Martin
#   5 Alpine   | 6 RB      | 7 Haas     | 8 McLaren   | 9 Audi (2026, ex-Sauber)
#   486 Cadillac (2026, 11. Team -> eigene F1-26-Team-Id, passt nur ins 2026-Format)
# compound (visual): 16=S 17=M 18=H 1=I 0=W
# "hidden": True  -> Name verdeckt (Spiel sendet Platzhalter) -> Overlay zeigt "Team #Nummer"
DRIVERS = [
    {"name": "Hypnotize",       "team_id": 2, "number": 1,  "compound": 17},  # Red Bull / M
    {"name": "pedroescrod98",   "team_id": 2, "number": 11, "compound": 17},  # Red Bull / M
    {"name": "lotowy",          "team_id": 1, "number": 16, "compound": 16},  # Ferrari / S
    {"name": "KingQuinn404",    "team_id": 9, "number": 27, "compound": 18},  # Audi / H
    {"name": "LitheColt",       "team_id": 6, "number": 22, "compound": 16},  # RB / S
    {"name": "Nicolashes",      "team_id": 7, "number": 20, "compound": 17},  # Haas / M
    {"name": "Borjita",         "team_id": 6, "number": 3,  "compound": 16},  # RB / S
    {"name": "SPIELER:IN",      "team_id": 0, "number": 44, "compound": 18, "hidden": True},   # Mercedes / H  -> "Mercedes #44"
    {"name": "Simon Dalbes",    "team_id": 5, "number": 10, "compound": 17},  # Alpine / M
    {"name": "Rensosuke",       "team_id": 0, "number": 63, "compound": 16},  # Mercedes / S
    {"name": "Player 1",        "team_id": 8, "number": 4,  "compound": 17, "hidden": True},   # McLaren / M  -> "McLaren #4"
    {"name": "Gabriel_flp_r",   "team_id": 1, "number": 55, "compound": 16},  # Ferrari / S
    {"name": "Gatuno",          "team_id": 1, "number": 14, "compound": 17},  # Ferrari / M
    {"name": "KO CleizyACM",    "team_id": 8, "number": 81, "compound": 16},  # McLaren / S
    {"name": "MidweekDread20",  "team_id": 7, "number": 31, "compound": 17},  # Haas / M
    {"name": "LF Laucha Vieja", "team_id": 3, "number": 23, "compound": 16},  # Williams / S
    {"name": "SPIELER:IN",      "team_id": 5, "number": 7,  "compound": 16, "hidden": True},   # Alpine / S  -> "Alpine #7"
    {"name": "C9 Guuveh",       "team_id": 4, "number": 18, "compound": 16},  # Aston Martin / S
    {"name": "gnalove",         "team_id": 9, "number": 24, "compound": 16},  # Audi / S
    {"name": "Xperj1_RD",       "team_id": 3,   "number": 2,  "compound": 16},  # Williams / S
    {"name": "TBD Cadillac 1",  "team_id": 486, "number": 33, "compound": 17},  # Cadillac / M  (Name/Nr. anpassen)
    {"name": "TBD Cadillac 2",  "team_id": 486, "number": 77, "compound": 16},  # Cadillac / S  (Name/Nr. anpassen)
]

# ── Header ────────────────────────────────────────────────────────────────────
HEADER_FORMAT = '<HBBBBBQfIIBB'
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

SESSION_UID = random.randint(1, 2_000_000_000)   # je Lauf neu -> main.py erkennt neue Session

def make_header(packet_id: int) -> bytes:
    return struct.pack(HEADER_FORMAT, GAME_FORMAT, 1, 0, 0, 1, packet_id, SESSION_UID, 0.0, 0, 0, 0, 255)

# ── Packet 4: Participants ────────────────────────────────────────────────────
# Offsets/Struct-Größe je nach GAME_FORMAT (siehe oben).
#   F1 25: team_id(uint8)@+3, race_number@+5, name@+7
#   F1 26: team_id(uint16)@+5, race_number@+8, name@+10

def make_participants() -> bytes:
    data = bytes([len(DRIVERS)])
    for i in range(NUM_CARS):
        pd = bytearray(PARTICIPANT_SIZE)
        if i < len(DRIVERS):
            d = DRIVERS[i]
            if IS_2026:
                struct.pack_into('<H', pd, P4_TEAM_OFFSET, d["team_id"])  # uint16
            else:
                pd[P4_TEAM_OFFSET] = d["team_id"] & 0xFF                  # uint8 (2026er-Ids wie 486 passen hier nicht)
            pd[P4_NUMBER_OFFSET] = d["number"]
            # Bei verdecktem Namen sendet das Spiel den lokalisierten Platzhalter
            disp = d["name"]
            name = disp.encode("utf-8")[:31]
            pd[P4_NAME_OFFSET:P4_NAME_OFFSET+len(name)] = name
        data += bytes(pd)
    return make_header(4) + data

# ── Packet 1: Session ─────────────────────────────────────────────────────────
# main.py liest: weather@+0, trackTemp@+1, airTemp@+2, totalLaps@+3,
#                sessionType@+6, formula@+8, timeLeft(uint16)@+9, safetyCar@+124
TOTAL_LAPS     = 18
QUALI_DURATION = 12 * 60  # 12 Minuten pro Q-Session

SESSION_TYPES = {
    "r":    (RACE_SESSION_ID, "Rennen (normal)"),
    "rsc":  (RACE_SESSION_ID, "Rennen + Safety Car"),
    "rvsc": (RACE_SESSION_ID, "Rennen + Virtual Safety Car"),
    "q1":   (5,  "Q1"),
    "q2":   (6,  "Q2"),
    "q3":   (7,  "Q3"),
}

# Erzwungener Safety-Car-Status je Modus: 0=keiner, 1=Safety Car, 2=Virtual SC
FORCED_SC  = {"r": 0, "rsc": 1, "rvsc": 2}
RACE_MODES = set(FORCED_SC)

# Wetter-Vorhersage fürs Overlay (m_numWeatherForecastSamples @126, dann 8-Byte-Samples).
# Demo-Szenario "Regen zieht auf" -> (timeOffset Minuten, weather, rain%).
# weather: 0 Klar, 1 leicht bewölkt, 2 bewölkt, 3 leichter Regen, 4 starker Regen, 5 Gewitter
WEATHER_FORECAST = [
    (0,  0, 0),     # jetzt (wird vom Overlay übersprungen)
    (5,  1, 15),
    (10, 2, 40),
    (15, 3, 65),
    (30, 4, 85),
    (60, 5, 70),
]

def sc_status_for(t: float, mode: str) -> int:
    """Erste 5s normal (damit der Glow-Effekt sichtbar einschaltet), danach Modus-Wert."""
    if mode not in RACE_MODES:
        return 0
    if t < 5:
        return 0
    return FORCED_SC[mode]

def make_session(t: float, mode: str, sc: int) -> bytes:
    sd = bytearray(220)
    sd[0] = 0     # weather: klar
    sd[1] = 39    # trackTemperature
    sd[2] = 25    # airTemperature
    session_id, _ = SESSION_TYPES[mode]
    sd[6] = session_id
    sd[8] = FORMULA_ID   # formula (13 = F1 26, 0 = F1)
    if mode in RACE_MODES:
        sd[3] = TOTAL_LAPS            # Rundenzahl in ALLEN Renn-Modi (auch mit SC/VSC)
        struct.pack_into('<H', sd, 9, 0)
    else:
        sd[3] = 0
        time_left = max(0, int(QUALI_DURATION - t))
        struct.pack_into('<H', sd, 9, time_left)
    sd[124] = sc  # safetyCarStatus (Offset 124, F1 25)
    # sd[125] = networkGame (0). Wetter-Vorhersage: Anzahl @126, dann 8-Byte-Samples.
    # Sample-Layout: sessionType@0, timeOffset@1, weather@2, trackTemp@3, airTemp@5, rain@7.
    sd[126] = len(WEATHER_FORECAST)
    for j, (toff, wx, rain) in enumerate(WEATHER_FORECAST):
        off = 127 + j * 8
        sd[off + 0] = session_id   # muss == aktueller Session sein, sonst filtert das Overlay
        sd[off + 1] = toff         # timeOffset (Minuten)
        sd[off + 2] = wx           # weather
        sd[off + 3] = 39           # trackTemperature (int8)
        sd[off + 5] = 25           # airTemperature (int8)
        sd[off + 7] = rain         # rainPercentage
    return make_header(1) + bytes(sd)

# ── Packet 2: Lap Data ────────────────────────────────────────────────────────
# main.py liest: lastLap(uint32)@+0, s1ms(uint16)@+8, s1min@+10, s2ms(uint16)@+11,
#                s2min@+13, gapFront(uint16)@+14, gapFrontMin@+16, gapLeader(uint16)@+17,
#                gapLeaderMin@+19, position@+32, currentLap@+33, pitStatus@+34, resultStatus@+45
LAP_DATA_SIZE = 57

# ── Renn-Simulation (realistisch) ─────────────────────────────────────────────
# Ablauf: enger Start  ->  Feld zieht auseinander  ->  Grüppchen/DRS-Züge bilden
# sich (Windschatten hält nahe Autos zusammen), einzelne mit Lücke nach vorn/hinten
#  ->  Boxenstopps (Auto fällt zurück, frische Reifen)  ->  DRS/MOM sobald < 1,0 s.
SIM_SPEED = 8.0     # 1 echte Sekunde = 8 Renn-Sekunden (Zeitraffer fürs Testen)
BASE_LAP  = 92.0    # Basis-Rundenzeit (s)
DRS_GAP   = 1.0     # Abstand zum Vordermann, ab dem DRS/MOM aktiv ist
PIT_LOSS  = 22.0    # Zeitverlust pro Boxenstopp (Renn-Sekunden)
N = len(DRIVERS)

random.seed()       # frische Werte je Lauf
_sim = {
    # Pace je Fahrer: leichte Staffelung (vorne schneller) + Zufall -> es bilden
    # sich Lücken, Grüppchen und einzelne Autos.
    "pace":  [BASE_LAP + 0.05 * i + random.uniform(-0.4, 0.8) for i in range(N)],
    "phase": [random.uniform(0, 6.283) for _ in range(N)],   # organisches Pace-Wobble
    "dist":  [-0.006 * i for i in range(N)],                 # Startaufstellung (eng)
    "comp":  [DRIVERS[i]["compound"] for i in range(N)],     # aktueller Reifen
    "tyre0": [0.0] * N,                                      # dist beim letzten Reifenwechsel
    "pit_lap":   {},                                         # geplante Box-Runde je Fahrer
    "pit_done":  [False] * N,
    "pit_until": [-1.0] * N,                                 # Renn-Zeit, bis wann in der Box
    "pit_secs":  [0.0] * N,                                  # Standzeit des Boxenstopps (s)
    "prev_rt":   0.0,
}
for i in range(N):                       # die meisten boxen einmal, gestaffelt
    if random.random() < 0.85:
        _sim["pit_lap"][i] = random.randint(5, 12)

_PIT_COMPOUND = {16: 17, 17: 18, 18: 17, 0: 17, 1: 17}   # S->M, M->H, H->M, sonst M
_frame = [dict() for _ in range(N)]      # je Frame berechneter Zustand pro Fahrer

def sim_step(real_t: float, mode: str):
    rt = real_t * SIM_SPEED
    s  = _sim
    dt = max(0.0, rt - s["prev_rt"])
    s["prev_rt"] = rt
    is_race = mode in RACE_MODES
    dist, pace = s["dist"], s["pace"]

    order  = sorted(range(N), key=lambda i: dist[i], reverse=True)  # führend zuerst
    pos_of = {drv: r + 1 for r, drv in enumerate(order)}

    for rank, i in enumerate(order):
        if rt < s["pit_until"][i]:
            continue   # steht in der Box -> kein Fortschritt -> fällt zurück
        tow = 0.0
        if rank > 0:                                   # Windschatten -> bildet Züge
            gap_s = (dist[order[rank - 1]] - dist[i]) * pace[i]
            if 0 < gap_s < DRS_GAP:
                tow = 0.05
        age   = dist[i] - s["tyre0"][i]
        fresh = max(0.0, 0.40 - 0.05 * age)            # frische Reifen kurz schneller
        eff   = pace[i] + 0.18 * math.sin(rt * 0.06 + s["phase"][i]) - tow - fresh
        dist[i] += dt / max(eff, 60.0)

        if is_race and i in s["pit_lap"] and not s["pit_done"][i] \
           and dist[i] > 0.3 and int(dist[i]) + 1 >= s["pit_lap"][i]:
            s["pit_done"][i]  = True
            s["pit_until"][i] = rt + PIT_LOSS
            s["tyre0"][i]     = dist[i]                 # frische Reifen
            s["comp"][i]      = _PIT_COMPOUND.get(s["comp"][i], 17)
            s["pit_secs"][i]  = random.uniform(2.0, 3.6)  # Standzeit

    if is_race:
        order = sorted(range(N), key=lambda i: dist[i], reverse=True)   # Rennen: nach Distanz
    else:
        order = sorted(range(N), key=lambda i: pace[i])                 # Quali: schnellste Pace = P1
    pos_of = {drv: r + 1 for r, drv in enumerate(order)}
    leader = order[0]
    for rank, i in enumerate(order):
        in_pit     = rt < s["pit_until"][i]
        gap_leader = (dist[leader] - dist[i]) * pace[i]
        gap_ahead  = 0.0 if rank == 0 else (dist[order[rank - 1]] - dist[i]) * pace[i]
        # In der Quali stabile Rundenzeit (= Pace) -> Bestrunde/Pole sauber; im Rennen mit Wobble.
        last_lap   = pace[i] + (0.18 * math.sin(rt * 0.06 + s["phase"][i]) if is_race else 0.0)
        _frame[i] = {
            "pos": pos_of[i],
            "gap_leader": max(0.0, gap_leader),
            "gap_ahead":  max(0.0, gap_ahead),
            "in_pit": in_pit,
            "drs": is_race and not in_pit and rank > 0 and 0 < gap_ahead < DRS_GAP,
            "lap": max(1, min(int(dist[i]) + 1, TOTAL_LAPS)),
            "last_lap": last_lap,
            "comp": s["comp"][i],
            "tyre_age": max(0, int(dist[i] - s["tyre0"][i])),
            "pit_time": s["pit_secs"][i],
        }

def _enc_gap(sec: float):
    """Sekunden -> (Minuten-Byte, Millisekunden-uint16), wie F1 die Lücke sendet."""
    sec = max(0.0, sec)
    m  = int(sec // 60)
    ms = int(round((sec - m * 60) * 1000))
    return min(m, 14), min(ms, 65535)

def make_lap_data(t: float, mode: str) -> bytes:
    sim_step(t, mode)
    data = b""
    for i in range(NUM_CARS):
        ld = bytearray(LAP_DATA_SIZE)
        if i < len(DRIVERS):
            f = _frame[i]
            lap_ms = int(f["last_lap"] * 1000)
            s1_ms  = int(f["last_lap"] * 0.30 * 1000)
            s2_ms  = int(f["last_lap"] * 0.36 * 1000)
            gf_m, gf_ms = _enc_gap(f["gap_ahead"])
            gl_m, gl_ms = _enc_gap(f["gap_leader"])

            struct.pack_into('<I', ld, 0,  lap_ms)
            struct.pack_into('<H', ld, 8,  s1_ms % 60000)
            ld[10] = s1_ms // 60000
            struct.pack_into('<H', ld, 11, s2_ms % 60000)
            ld[13] = s2_ms // 60000
            struct.pack_into('<H', ld, 14, gf_ms)
            ld[16] = gf_m
            struct.pack_into('<H', ld, 17, gl_ms)
            ld[19] = gl_m
            ld[32] = f["pos"]
            ld[33] = f["lap"]
            ld[34] = 2 if f["in_pit"] else 0   # pitStatus
            ld[35] = 1 if _sim["pit_done"][i] else 0   # m_numPitStops (Sim: max. 1 Stopp)
            # driverStatus: 0=Box/Garage, 3=Outlap, 4=OnTrack(Hotlap)
            if f["in_pit"]:
                ld[44] = 0
            elif mode in RACE_MODES:
                ld[44] = 3 if i % 4 == 0 else 4
            else:
                # Quali: Status zyklisch je Fahrer versetzt (Hotlap -> Outlap -> Box),
                # damit man Live-Sektoren (On Track) UND Best-Sektoren (Box/Outlap) sieht.
                phase = (t * 0.5 + i * 1.7) % 6.0
                ld[44] = 4 if phase < 3.0 else (3 if phase < 4.5 else 0)
            # m_currentLapInvalid: in der Quali gehen On-Track-Fahrer zyklisch "invalid"
            # (Track Limits) -> Zeiten werden im Overlay rot.
            if mode not in RACE_MODES and ld[44] == 4 and int(t * 0.8 + i * 2) % 9 < 2:
                ld[37] = 1
            ld[45] = 2                          # resultStatus = aktiv
            struct.pack_into('<H', ld, 49, min(int(f["pit_time"] * 1000), 65535))  # m_pitStopTimerInMS
        data += bytes(ld)
    return make_header(2) + data

# ── Packet 7: Car Status ──────────────────────────────────────────────────────
# main.py liest: visualCompound@+26, tyreAge@+27, ersStore(float)@+37
# (CAR_STATUS_SIZE wird oben je nach GAME_FORMAT gesetzt)
ERS_MAX = 4_000_000.0

def make_car_status(t: float) -> bytes:
    data = b""
    for i in range(NUM_CARS):
        cs = bytearray(CAR_STATUS_SIZE)
        if i < len(DRIVERS):
            f = _frame[i]
            cs[26] = f.get("comp", DRIVERS[i]["compound"])  # visualCompound
            cs[27] = min(f.get("tyre_age", 0), 99)          # tyreAge (Runden)
            ers = ERS_MAX * (0.5 + 0.45 * math.sin(t * 0.25 + i * 0.7))
            struct.pack_into('<f', cs, 37, ers)
        data += bytes(cs)
    return make_header(7) + data

# ── Packet 6: Car Telemetry ───────────────────────────────────────────────────
# main.py liest: drs@+18  (CAR_TELEM_SIZE wird oben je nach GAME_FORMAT gesetzt)

def make_car_telemetry(t: float) -> bytes:
    data = b""
    for i in range(NUM_CARS):
        ct = bytearray(CAR_TELEM_SIZE)
        if i < len(DRIVERS):
            ct[18] = 1 if _frame[i].get("drs") else 0   # DRS/MOM aktiv (< 1,0 s)
        data += bytes(ct)
    return make_header(6) + data

# Packet 16: Car Telemetry 2 (2026-Pack) -> Overtake Mode (Ersatz für DRS).
# verfügbar wenn Abstand < 1,0 s, aktiv wenn < 0,6 s (näher dran = "boostet").
def make_car_telemetry2(mode: str) -> bytes:
    is_race = mode in RACE_MODES
    data = b""
    for i in range(NUM_CARS):
        ct = bytearray(CAR_TELEM2_SIZE)
        if i < len(DRIVERS):
            f = _frame[i]
            ga = f.get("gap_ahead", 99.0)
            free = is_race and not f.get("in_pit") and ga > 0
            ct[4] = 1 if (free and ga < 1.0) else 0   # m_overtakeAvailable
            ct[5] = 1 if (free and ga < 0.6) else 0   # m_overtakeActive
            ct[8] = 1                                  # m_2026Regulations
        data += bytes(ct)
    return make_header(16) + data

def make_event(code: str, details: bytes = b"") -> bytes:
    body = code.encode("ascii")[:4].ljust(4, b"\x00") + details
    return make_header(3) + body

SC_LABEL = {0: "—", 1: "SAFETY CAR", 2: "VIRTUAL SC"}

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 52)
    print("  MRL Telemetry Test Sender v3")
    print(f"  -> {UDP_IP}:{UDP_PORT}  |  {len(DRIVERS)} Fahrer")
    print(f"  -> Format {GAME_FORMAT} ({'F1 26' if IS_2026 else 'F1 25'})  |  {NUM_CARS} Auto-Slots")
    print("=" * 52)
    print("  [R]    Rennen (normal, kein Safety Car)")
    print("  [RSC]  Rennen mit Safety Car")
    print("  [RVSC] Rennen mit Virtual Safety Car")
    print("  [Q1]   Qualifying 1")
    print("  [Q2]   Qualifying 2")
    print("  [Q3]   Qualifying 3")
    print("=" * 52)
    while True:
        choice = input("  Modus waehlen (R/RSC/RVSC/Q1/Q2/Q3): ").strip().lower()
        if choice in SESSION_TYPES:
            break
    SESSION_MODE = choice
    _, mode_name = SESSION_TYPES[SESSION_MODE]
    hidden = [d["name"] for d in DRIVERS if d.get("hidden")]
    print(f"  -> Starte {mode_name}")
    print(f"  -> Verdeckte Namen (Fallback-Test): {hidden}")
    if SESSION_MODE in ("rsc", "rvsc"):
        print("  -> Ab Sekunde 5 dauerhaft aktiv -> Glow-Rahmen im Overlay")
    if SESSION_MODE in RACE_MODES:
        print(f"  -> Realistische Sim: enger Start -> Gruppen/DRS-Zuege -> Boxenstopps; DRS/MOM < {DRS_GAP:.0f}s")
        print(f"  -> Boxenstopp-Runden je Fahrer: {dict(sorted(_sim['pit_lap'].items()))}")
    print("  Strg+C zum Beenden")
    print("=" * 52)

    # Erstpakete
    sock.sendto(make_participants(), (UDP_IP, UDP_PORT))
    sock.sendto(make_session(0, SESSION_MODE, 0), (UDP_IP, UDP_PORT))
    print("[P4] Participants gesendet | [P1] Session gesendet")
    time.sleep(0.3)

    tick     = 0
    start    = time.time()
    last_sc  = -1

    while True:
        t  = time.time() - start
        sc = sc_status_for(t, SESSION_MODE)

        if tick % 100 == 0:
            sock.sendto(make_participants(), (UDP_IP, UDP_PORT))
        if tick % 10 == 0:
            sock.sendto(make_session(t, SESSION_MODE, sc), (UDP_IP, UDP_PORT))

        sock.sendto(make_lap_data(t, SESSION_MODE), (UDP_IP, UDP_PORT))
        sock.sendto(make_car_status(t),             (UDP_IP, UDP_PORT))
        sock.sendto(make_car_telemetry(t),          (UDP_IP, UDP_PORT))
        if IS_2026:   # Overtake Mode (DRS-Ersatz) nur im 2026-Pack-Format
            sock.sendto(make_car_telemetry2(SESSION_MODE), (UDP_IP, UDP_PORT))

        # Rennleitungs-Events fürs Overlay (nur Rennen): MOM aktiv + ab und zu eine Strafe
        if SESSION_MODE in RACE_MODES:
            if tick == 40:
                sock.sendto(make_event("DRSE"), (UDP_IP, UDP_PORT))
            elif tick > 60 and tick % 220 == 0:
                vi = random.randint(0, len(DRIVERS) - 1)
                ptype, ptime = random.choice([(4, 5), (5, 0), (10, 0)])  # Zeitstrafe / Verwarnung / Track Limits
                sock.sendto(make_event("PENA", bytes([ptype, 0, vi, 255, ptime, 0, 0])), (UDP_IP, UDP_PORT))

        # Safety-Car-Wechsel hervorheben
        if sc != last_sc:
            print(f"[{t:6.1f}s] >>> Safety-Car-Status: {SC_LABEL[sc]}")
            last_sc = sc

        if tick % 20 == 0:
            if SESSION_MODE in RACE_MODES:
                lap = max((f.get("lap", 1) for f in _frame), default=1)
                print(f"[{t:6.1f}s] Tick {tick:4d} | Runde {lap}/{TOTAL_LAPS} | SC: {SC_LABEL[sc]}")
            else:
                tl = max(0, int(QUALI_DURATION - t))
                print(f"[{t:6.1f}s] Tick {tick:4d} | {mode_name} verbleibend {tl//60}:{tl%60:02d}")

        tick += 1
        time.sleep(0.05)  # 20 Hz

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
