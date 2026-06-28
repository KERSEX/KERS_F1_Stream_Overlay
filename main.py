"""
MRL Overlay – F1 26 (UDP-Format "2026") Variante von main.py.

Identische Funktion wie main.py, aber an die F1 26 / 2026-Season-Pack
Spezifikation angepasst. Wichtigste Unterschiede gegenüber F1 25:
  * Max. 24 Autos statt 22 (neues Team fürs 2026er Feld)
  * Participant: Driver-/Network-/Team-Id sind jetzt uint16 -> Struct 57 -> 60 Bytes,
    Name-Offset 7 -> 10, Team-Offset 3 -> 5 (uint16!), Startnummer-Offset 5 -> 8
  * Car Status: neues Feld m_ersHarvestLimitPerLap -> Struct 55 -> 59 Bytes
    (die hier gelesenen Offsets 26/27/37 bleiben gleich)
  * Car Telemetry: m_engineTemperature uint16 -> uint8 -> Struct 60 -> 59 Bytes
    (DRS bleibt an Offset 18)
  * Lap Data und Session-Offsets sind unverändert (Safety Car weiterhin +124)
  * Formula 13 = "F1 26", neue '26-Team-Ids (476-486, inkl. Audi & Cadillac)

Im Spiel muss UDP-Format auf "2026" stehen.
"""

import json, os, socket, struct, threading, time, webbrowser
from flask import Flask, render_template, jsonify

app = Flask(__name__)

UDP_IP   = "0.0.0.0"
UDP_PORT = 20777

HEADER_FORMAT = '<HBBBBBQfIIBB'
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)

# Dieses Skript parst ausschliesslich das F1 26 / 2026-Format. Sendet das Spiel
# 2025 oder 2024, sind alle Offsets verschoben (Driver/Team-Id uint8 statt uint16,
# Name-Offset 7 statt 10, andere Strides) -> Namen/Teams/Reifen werden Muell.
# Dann im Spiel UDP-Format auf "2026" stellen (oder main.py fuer 2025 nutzen).
EXPECTED_FORMAT = 2026

# F1 26: bis zu 24 Autos in allen Arrays
MAX_CARS = 24

COMPOUND = {0: "W", 1: "I", 2: "E", 16: "S", 17: "M", 18: "H", 7: "I", 8: "W"}
COMPOUND_COLOR = {"S": "#FF3333", "M": "#FFDD00", "H": "#FFFFFF", "I": "#39CFFF", "W": "#4499FF", "E": "#FF9900"}
DRS_ACTIVE = {10, 12, 14}
state_lock = threading.Lock()
drivers: dict[int, dict] = {}
last_packet_time = 0.0
# Car-Indizes, die laut Participants-Paket ein ECHTES Team tragen. Unbelegte Slots
# markiert das Spiel mit team_id 255/65535 -> die fliegen raus. Index-genau (nicht
# über m_numActiveCars), damit Fahrer auf hohen Indizes in Online-Lobbys drinbleiben.
valid_participants: set = set()
INVALID_TEAM_IDS = {255, 65535}
_last_session_uid = None   # erkennt Session-Wechsel -> Runden/FL/Fahrer zurücksetzen
race_control: list = []     # Rennleitungs-Meldungen (Strafen, MOM/Override, ...) fürs Overlay
_rc_id = 0
PENALTY_LABELS = {0: "Durchfahrtsstrafe", 1: "Stop-and-Go", 2: "Startplatzstrafe",
                  4: "Zeitstrafe", 5: "Verwarnung", 6: "Disqualifiziert", 9: "Reifen-Regel"}

WEATHER = {0: "Klar ☀️", 1: "Leicht bewölkt 🌤️", 2: "Bewölkt ☁️", 3: "Leichter Regen 🌦️", 4: "Starker Regen 🌧️", 5: "Gewitter ⛈️"}
WEATHER_EMOJI = {0: "☀️", 1: "🌤️", 2: "☁️", 3: "🌦️", 4: "🌧️", 5: "⛈️"}
SESSION_TYPE = {0: "Unbekannt", 1: "P1", 2: "P2", 3: "P3", 4: "Short P", 5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Q", 9: "OSQ", 10: "SSO1", 11: "SSO2", 12: "SSO3", 13: "Short SSO", 14: "OSSO", 15: "Rennen", 16: "Rennen 2", 17: "Rennen 3", 18: "Zeitrennen", 255: "Unbekannt"}
QUALI_TYPES = {5, 6, 7, 8, 9}
FORMULA_NAMES = {0: "F1", 1: "F1 Classic", 2: "F2", 3: "F1 Generic", 4: "Beta", 6: "Esports", 8: "F1W", 9: "F1 Elimination", 13: "F1 26"}
# Safety car status: 0=none, 1=Full SC, 2=VSC, 3=Formation Lap
SC_STATUS = {0: "none", 1: "sc", 2: "vsc", 3: "formation"}

session_info = {
    "type": 0,
    "type_name": "Unbekannt",
    "weather": 0,
    "weather_name": "Klar ☀️",
    "weather_emoji": "☀️",
    "weather_rain": 0,
    "track_temp": 0,
    "air_temp": 0,
    "total_laps": 0,
    "current_lap": 0,
    "fastest_lap_time": 0.0,
    "fastest_lap_driver": "",
    "is_quali": False,
    "time_left": 0,
    "formula": 0,
    "formula_name": "F1",
    "safety_car": 0,
    "safety_car_status": "none",
    "forecast": []
}

# Team-Ids laut F1 26 Appendix. Sowohl die Basis-Ids (0-9) als auch die
# eigenständigen '26-Ids (476-486) werden gemappt, damit es egal ist welche
# das Spiel im 2026-Season-Pack sendet.
TEAM_NAMES = {
    0: "Mercedes", 1: "Ferrari", 2: "Red Bull", 3: "Williams", 4: "Aston Martin",
    5: "Alpine", 6: "RB", 7: "Haas", 8: "McLaren", 9: "Audi",  # 2026: Sauber -> Audi
    41: "F1 Generic", 104: "Custom Team",
    # 2026er Feld
    476: "Mercedes", 477: "Ferrari", 478: "Red Bull", 479: "Williams", 480: "Aston Martin",
    481: "Alpine", 482: "RB", 483: "Haas", 484: "McLaren", 485: "Audi", 486: "Cadillac",
}

def get_driver(idx):
    if idx not in drivers:
        drivers[idx] = {"index": idx, "name": "", "team": "", "team_id": -1, "position": 0, "gap_to_leader": 0.0, "gap_to_ahead": 0.0, "compound": "?", "tyre_age": 0, "last_lap": 0.0, "sector1": 0.0, "sector2": 0.0, "drs": False, "ers_pct": 0.0, "ers_mode": 0, "in_pit": False, "dnf": False, "dsq": False, "race_number": 0, "best_lap": 0.0, "driver_status": 0, "pit_time": 0.0, "overtake_active": False, "overtake_available": False,
                       "lap_invalid": False, "pit_stops": 0, "stints": []}
    return drivers[idx]

def parse_header(data):
    if len(data) < HEADER_SIZE:
        return None
    h = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    return {"format": h[0], "packet_id": h[5], "session_uid": h[6]}

# F1 26 ParticipantData: 60 Bytes (Driver/Network/Team-Id jetzt uint16)
PARTICIPANT_SIZE = 60
P4_NAME_OFFSET = 10   # char m_name[32]
P4_TEAM_OFFSET = 5    # uint16 m_teamId
P4_NUMBER_OFFSET = 8  # uint8 m_raceNumber

# Platzhalter-Namen verdeckter Spieler (je nach Spielsprache), evtl. mit Nummer dahinter.
PLACEHOLDER_NAMES = {"player", "spieler", "spielerin", "spieler:in", "spieler in"}

def is_placeholder_name(name):
    """True wenn der Name verdeckt ist: leer oder z.B. 'Player', 'Player 1', 'SPIELER:IN', 'Spieler:in 2'."""
    n = name.strip().lower()
    if not n:
        return True
    # evtl. nachgestellte Nummer/Leerzeichen entfernen ("Player 1" -> "player")
    core = n.rstrip("0123456789 ").strip()
    return core in PLACEHOLDER_NAMES

def build_fallback_name(team_name, race_number, formula_prefix):
    """Anzeigename wenn kein freigegebener Online-Name vorliegt -> z.B. 'Ferrari #11'."""
    if team_name and race_number > 0:
        return f"{team_name} #{race_number}"
    if team_name:
        return team_name
    if race_number > 0:
        return f"{formula_prefix} #{race_number}"
    return f"{formula_prefix} #?"

def handle_session(data):
    base = HEADER_SIZE
    if len(data) < base + 125:
        return
    weather      = data[base + 0]
    track_temp   = data[base + 1]
    air_temp     = data[base + 2]
    total_laps   = data[base + 3]
    session_type = data[base + 6]
    formula      = data[base + 8]
    _tl_raw = struct.unpack_from('<H', data, base + 9)[0] if len(data) >= base + 11 else 0
    time_left = _tl_raw if _tl_raw <= 3600 else 0
    # m_safetyCarStatus (F1 26): 19 feste Bytes (inkl. uint16 m_trackLength) + 21 MarshalZones
    # (je 5 Bytes = 105) = Offset 124. Unverändert ggü. F1 25 (Marshal-Zones sind nicht
    # an die Autoanzahl gekoppelt).
    safety_car = data[base + 124]

    # Wetter-Vorhersage: m_numWeatherForecastSamples @ +126, danach 8-Byte-Samples.
    # Sample-Layout: sessionType(0), timeOffset min(1), weather(2), ..., rainPercentage(7).
    forecast = []
    current_rain = 0
    if len(data) >= base + 127:
        num_fc = data[base + 126]
        fc_base = base + 127
        for i in range(min(num_fc, 56)):
            off = fc_base + i * 8
            if len(data) < off + 8:
                break
            fc_session  = data[off + 0]
            time_offset = data[off + 1]
            fc_weather  = data[off + 2]
            rain        = data[off + 7]
            if fc_session != session_type:
                continue
            # Jetzt-Wert (0 min) liefert den aktuellen Regen fürs "Jetzt"-Feld
            if time_offset == 0:
                current_rain = rain
                continue
            forecast.append({
                "t": time_offset,
                "weather": fc_weather,
                "emoji": WEATHER_EMOJI.get(fc_weather, "☀️"),
                "rain": rain,
            })
            if len(forecast) >= 5:
                break

    session_info.update({
        "type": session_type,
        "type_name": SESSION_TYPE.get(session_type, "Unbekannt"),
        "weather": weather,
        "weather_name": WEATHER.get(weather, "Klar ☀️"),
        "weather_emoji": WEATHER_EMOJI.get(weather, "☀️"),
        "weather_rain": current_rain,
        "track_temp": track_temp,
        "air_temp": air_temp,
        "total_laps": total_laps,
        "is_quali": session_type in QUALI_TYPES,
        "time_left": time_left,
        "formula": formula,
        "formula_name": FORMULA_NAMES.get(formula, "F1"),
        "safety_car": safety_car,
        "safety_car_status": SC_STATUS.get(safety_car, "none"),
        "forecast": forecast
    })

def handle_participants(data):
    base = HEADER_SIZE + 1
    formula_prefix = FORMULA_NAMES.get(session_info.get("formula", 0), "F1")
    valid_participants.clear()
    for i in range(MAX_CARS):
        start = base + i * PARTICIPANT_SIZE
        if start + PARTICIPANT_SIZE > len(data):
            break
        team_id      = struct.unpack_from('<H', data, start + P4_TEAM_OFFSET)[0]  # uint16 in F1 26
        # Unbelegter Slot (Spiel füllt mit 0xFF -> 65535, bzw. 255) -> kein echter Fahrer.
        if team_id in INVALID_TEAM_IDS:
            drivers.pop(i, None)
            continue
        valid_participants.add(i)
        race_number  = data[start + P4_NUMBER_OFFSET]
        name_bytes   = data[start + P4_NAME_OFFSET: start + P4_NAME_OFFSET + 32]
        name         = name_bytes.decode("utf-8", "ignore").split("\x00", 1)[0].strip()
        team_name    = TEAM_NAMES.get(team_id, "")
        team_display = team_name if team_name else f"T{team_id}"

        # Echten Online-Namen immer nutzen, wenn vorhanden. Nur wenn der Spieler ihn
        # verdeckt, sendet das Spiel "Player"/leer -> Team + Startnummer (z.B. "Ferrari #11").
        name_hidden = is_placeholder_name(name)
        if name_hidden:
            name = build_fallback_name(team_name, race_number, formula_prefix)

        d = get_driver(i)
        d["name"]        = name
        d["team"]        = team_display
        d["team_id"]     = team_id
        d["race_number"] = race_number

# F1 26 LapData: unverändert 57 Bytes, gleiche Offsets wie F1 25
LAP_DATA_SIZE = 57

def handle_lap_data(data):
    base = HEADER_SIZE
    max_lap = 0
    for i in range(MAX_CARS):
        start = base + i * LAP_DATA_SIZE
        if start + LAP_DATA_SIZE > len(data):
            break
        try:
            last_lap_ms = struct.unpack_from('<I', data, start + 0)[0]
            s1_ms = struct.unpack_from('<H', data, start + 8)[0]
            s1_min = data[start + 10]
            s2_ms = struct.unpack_from('<H', data, start + 11)[0]
            s2_min = data[start + 13]
            gap_front  = struct.unpack_from('<H', data, start + 14)[0]
            gap_front_m = data[start + 16]
            gap_leader  = struct.unpack_from('<H', data, start + 17)[0]
            gap_leader_m = data[start + 19]
            position = data[start + 32]
            pit_status = data[start + 34]
            num_pit_stops = data[start + 35]   # m_numPitStops
            lap_invalid = data[start + 37]     # m_currentLapInvalid (0=gültig, 1=ungültig)
            driver_status = data[start + 44]   # 0=Garage,1=fliegende Runde,2=in lap,3=out lap,4=on track
            result_stat = data[start + 45]
            pit_time_ms = struct.unpack_from('<H', data, start + 49)[0]   # m_pitStopTimerInMS (Standzeit)
        except struct.error:
            continue
        # Leere Slots überspringen: Position 0, result_status 0 (= INVALID) oder ein
        # Slot, den das Participants-Paket nicht als echten Teilnehmer geführt hat
        # (team_id 255/65535). Pro-Auto + index-genau -> echte Fahrer auf hohen
        # Car-Indizes (Online-Lobby!) bleiben drin, Phantom-Slots fliegen raus.
        if position == 0 or result_stat == 0:
            continue
        if valid_participants and i not in valid_participants:
            continue
        d = get_driver(i)
        if 30000 < last_lap_ms < 600000:
            d["last_lap"] = last_lap_ms / 1000.0
            lap_s = last_lap_ms / 1000.0
            if (session_info["fastest_lap_time"] == 0.0 or lap_s < session_info["fastest_lap_time"]) and d["name"]:
                session_info["fastest_lap_time"] = lap_s
                session_info["fastest_lap_driver"] = d["name"]
            if d["best_lap"] == 0.0 or lap_s < d["best_lap"]:   # Bestrunde des Fahrers (Quali)
                d["best_lap"] = lap_s
        s1 = s1_min * 60 + s1_ms / 1000.0
        s2 = s2_min * 60 + s2_ms / 1000.0
        d["sector1"] = s1 if 0 < s1 < 300 else 0.0
        d["sector2"] = s2 if 0 < s2 < 300 else 0.0
        # Gap: minute part should be 0 in normal racing, >5min is impossible
        ahead_min  = gap_front_m  if gap_front_m  < 5 else 0
        leader_min = gap_leader_m if gap_leader_m < 5 else 0
        d["gap_to_ahead"]  = ahead_min  * 60.0 + gap_front  / 1000.0
        d["gap_to_leader"] = leader_min * 60.0 + gap_leader / 1000.0
        d["position"] = position
        d["in_pit"] = pit_status in (1, 2)
        d["pit_stops"] = num_pit_stops
        d["lap_invalid"] = lap_invalid == 1
        d["dnf"] = result_stat in (4, 6, 7)
        d["dsq"] = result_stat == 5
        d["driver_status"] = driver_status
        if pit_time_ms > 0:                 # Standzeit des letzten Boxenstopps (sticky)
            d["pit_time"] = pit_time_ms / 1000.0

        # Fallback-Name falls noch keiner gesetzt (z.B. Lap-Daten vor Participants-Paket)
        if not d["name"] and position > 0:
            team = TEAM_NAMES.get(d.get("team_id", -1), d.get("team", ""))
            formula_prefix = FORMULA_NAMES.get(session_info.get("formula", 0), "F1")
            d["name"] = build_fallback_name(team, d.get("race_number", 0), formula_prefix)

        if position > 0:
            lap_num = data[start + 33]
            if lap_num > max_lap:
                max_lap = lap_num
    # current_lap = Runde des Führenden, jeden Frame neu gesetzt (nicht monoton) ->
    # startet bei 1 und resettet automatisch, wenn ein neues Rennen bei Runde 1 beginnt.
    if max_lap > 0:
        session_info["current_lap"] = max_lap

# F1 26 CarStatusData: 59 Bytes (m_ersHarvestLimitPerLap neu).
# Die hier gelesenen Felder liegen vor dem neuen Feld -> Offsets unverändert.
CAR_STATUS_SIZE = 59
ERS_MAX = 4_000_000.0

def handle_car_status(data):
    base = HEADER_SIZE
    for i in range(MAX_CARS):
        start = base + i * CAR_STATUS_SIZE
        if start + CAR_STATUS_SIZE > len(data):
            break
        try:
            visual_comp = data[start + 26]
            tyre_age = data[start + 27]
            ers_store = struct.unpack_from('<f', data, start + 37)[0]
            ers_mode = data[start + 41]   # m_ersDeployMode: 0=none,1=medium,2=hotlap,3=overtake(=MOM/Override)
        except (struct.error, IndexError):
            continue
        d = get_driver(i)
        d["compound"] = COMPOUND.get(visual_comp, "?")
        d["tyre_age"] = tyre_age
        d["ers_pct"] = round(min(max(ers_store / ERS_MAX * 100, 0), 100), 1)
        d["ers_mode"] = ers_mode
        # Stint-Historie: bei (gültigem) Compound-Wechsel anhängen -> Box-Strategie
        c = d["compound"]
        if c in ("S", "M", "H", "I", "W", "E"):
            stints = d["stints"]
            if not stints or stints[-1] != c:
                stints.append(c)
                if len(stints) > 6:
                    del stints[0]

# F1 26 CarTelemetryData: 59 Bytes (m_engineTemperature jetzt uint8).
# DRS bleibt an Offset 18.
CAR_TELEM_SIZE = 59

def handle_car_telemetry(data):
    base = HEADER_SIZE
    for i in range(MAX_CARS):
        start = base + i * CAR_TELEM_SIZE
        if start + 19 > len(data):
            break
        try:
            drs_status = data[start + 18]
        except IndexError:
            continue
        get_driver(i)["drs"] = drs_status == 1

# F1 26 (2026-Pack) CarTelemetry2Data: 10 Bytes pro Auto, neues Paket (pid 16).
# Hier steckt der 2026 "Overtake Mode" (Ersatz für DRS):
#   m_overtakeAvailable @+4 (0/1), m_overtakeActive @+5 (0/1).
# (Davor: activeAeroMode @0, activeAeroAvailable @1, activeAeroActivationDistance @2-3.)
CAR_TELEM2_SIZE = 10

def handle_car_telemetry2(data):
    base = HEADER_SIZE
    for i in range(MAX_CARS):
        start = base + i * CAR_TELEM2_SIZE
        if start + CAR_TELEM2_SIZE > len(data):
            break
        d = get_driver(i)
        d["overtake_available"] = data[start + 4] == 1
        d["overtake_active"]    = data[start + 5] == 1

def push_rc(type_, text):
    global _rc_id
    _rc_id += 1
    race_control.append({"id": _rc_id, "type": type_, "text": text})
    if len(race_control) > 12:
        race_control.pop(0)

# Packet 3: Event – Strafen / Track-Limits, DRS bzw. MOM/Override aktiviert.
def handle_event(data):
    if len(data) < HEADER_SIZE + 4:
        return
    code = data[HEADER_SIZE:HEADER_SIZE + 4].decode("ascii", "ignore")
    if code == "PENA" and len(data) >= HEADER_SIZE + 11:
        b = HEADER_SIZE + 4
        pen_type, veh, pen_time = data[b], data[b + 2], data[b + 4]
        name = (drivers.get(veh) or {}).get("name") or f"Auto #{veh}"
        if pen_type in (10, 11, 12, 13, 14):   # Runde(n) annulliert = Track Limits
            push_rc("tracklimit", f"{name}: Track Limits")
        else:
            label = PENALTY_LABELS.get(pen_type, "Strafe")
            if pen_type == 4 and pen_time > 0:
                label += f" +{pen_time}s"
            push_rc("penalty", f"{name}: {label}")
    elif code == "DRSE":
        push_rc("mom", "MOM / OVERRIDE AKTIV" if session_info.get("formula") == 13 else "DRS AKTIV")
    elif code == "DRSD":
        push_rc("mom", "MOM / OVERRIDE AUS" if session_info.get("formula") == 13 else "DRS AUS")

_debug_count = 0
_warned_format = None

def udp_listener():
    global last_packet_time, _debug_count, _warned_format, _last_session_uid
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((UDP_IP, UDP_PORT))
        print(f"[UDP] Listening on :{UDP_PORT} (F1 26 / Format 2026)")
    except Exception as e:
        print(f"[UDP] FEHLER beim Binden: {e}")
        return
    while True:
        try:
            data, addr = sock.recvfrom(8192)
            h = parse_header(data)
            if not h:
                continue
            pid = h["packet_id"]
            print(f"[PKT] pid={pid} len={len(data)} fmt={h['format']}")
            if h["format"] != EXPECTED_FORMAT and h["format"] != _warned_format:
                print("=" * 70)
                print(f"[WARN] UDP-Format {h['format']} empfangen, 26.py erwartet {EXPECTED_FORMAT}!")
                print("       -> Im Spiel UDP-Format auf '2026' stellen, sonst sind")
                print("          Namen/Teams/Reifen verschoben (oder main.py fuer 2025 nutzen).")
                print("=" * 70)
                _warned_format = h["format"]
            with state_lock:
                # Neue Session (anderes session_uid) -> Runden/FL/Fahrer zurücksetzen.
                if h["session_uid"] != _last_session_uid:
                    _last_session_uid = h["session_uid"]
                    drivers.clear()
                    valid_participants.clear()
                    session_info["current_lap"]        = 0
                    session_info["fastest_lap_time"]   = 0.0
                    session_info["fastest_lap_driver"] = ""
                    race_control.clear()
                last_packet_time = time.time()
                if pid == 1:
                    handle_session(data)
                    print(f"[P1] Formula={session_info['formula_name']} SC={session_info['safety_car_status']}")
                elif pid == 4:
                    handle_participants(data)
                    names = [d["name"] for d in drivers.values() if d["name"]]
                    print(f"[P4] {len(names)} Namen: {names[:3]}")
                elif pid == 2:
                    handle_lap_data(data)
                    active = [d for d in drivers.values() if d["position"] > 0]
                    print(f"[P2] {len(active)} aktive Fahrer")
                    for d in active[:3]:
                        print(f"     P{d['position']}: '{d['name']}' pos={d['position']}")
                elif pid == 7:
                    handle_car_status(data)
                elif pid == 6:
                    handle_car_telemetry(data)
                elif pid == 16:
                    handle_car_telemetry2(data)   # 2026: Overtake Mode (DRS-Ersatz)
                elif pid == 3:
                    handle_event(data)
        except Exception as e:
            print(f"[UDP] Error: {e}")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/live")
def api_live():
    with state_lock:
        formula_prefix = FORMULA_NAMES.get(session_info.get("formula", 0), "F1")
        active = []
        for d in drivers.values():
            if d["position"] > 0 and (not valid_participants or d["index"] in valid_participants):
                entry = dict(d)
                if not entry["name"]:
                    team = TEAM_NAMES.get(entry.get("team_id", -1), entry.get("team", ""))
                    entry["name"] = build_fallback_name(team, entry.get("race_number", 0), formula_prefix)
                active.append(entry)
        active.sort(key=lambda x: x["position"])
        connected = (time.time() - last_packet_time) < 3.0
    return jsonify({"drivers": active, "connected": connected, "session": session_info, "race_control": list(race_control)})

@app.route("/api/status")
def api_status():
    connected = (time.time() - last_packet_time) < 3.0
    return jsonify({"connected": connected, "driver_count": len(drivers)})

if __name__ == "__main__":
    t = threading.Thread(target=udp_listener, daemon=True)
    t.start()
    webbrowser.open("http://127.0.0.1:5100")
    app.run(host="0.0.0.0", port=5100, debug=False, use_reloader=False)
