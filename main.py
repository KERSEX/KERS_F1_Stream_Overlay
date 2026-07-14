"""
KERS F1 Overlay – F1 26 (UDP-Format "2026") Variante von main.py.

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

import gzip, json, os, socket, struct, threading, time, webbrowser
from flask import Flask, render_template, jsonify, Response, request

app = Flask(__name__)

@app.after_request
def _no_cache(resp):
    # API-Antworten NIE cachen -> sonst zeigt das Overlay veraltete Trackmap/Standings
    # (der Browser servierte /api/track sonst aus dem Cache und ignorierte neue Daten).
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp
# Templates bei jeder Änderung neu laden (sonst cached Flask bei debug=False das
# Template beim ersten Aufruf für immer -> HTML/CSS-Änderungen wären erst nach
# Server-Neustart sichtbar). Der mtime-Check kostet praktisch nichts.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
# Statische Dateien (Logos, Fonts) nicht lange cachen -> Änderungen schlagen schneller durch.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# Debug-Ausgaben (Paket-Logs). Im Normalbetrieb AUS lassen: die prints laufen sonst
# für jedes UDP-Paket (bis zu 60/s) und bremsen den Listener auf Windows spürbar.
DEBUG = False

# ── Overlay-Settings ──────────────────────────────────────────────────────────
# Serverseitig gespeichert (overlay_settings.json) und live über /settings (auch vom
# Laptop/Handy im Netz) änderbar. Das Overlay bekommt sie mit jedem Payload und wendet
# sie sofort an. URL-Parameter im Overlay überschreiben einzelne Werte weiterhin.
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_settings.json")
CHAMP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "championship.json")   # A3: WM-Stand (manuell gepflegt)
POINTS_SYSTEM = {1: 25, 2: 18, 3: 15, 4: 12, 5: 10, 6: 8, 7: 6, 8: 4, 9: 2, 10: 1}   # A3: Punkte Top 10 (Logik aus KERS_WM_Overlay)
DEFAULT_SETTINGS = {
    # Sichtbarkeit der Komponenten
    "tower": True, "battles": True, "map": True, "onboard": True,
    "lights": True, "damage": True, "msgs": True, "ticker": True,
    "lowerthird": True, "flbanner": True,
    "undercut": True, "deltabar": True, "mapnumbers": True, "mapflags": True,
    "pred": True, "danger": True, "comeback": True, "pbflash": True, "battlemap": True, "fresh": True,
    "strat": True, "pitproj": True,
    "brand_title": "", "brand_accent": "#e10600",   # A2: Branding (Titel standardmäßig aus)
    "header_color": "", "row_color": "",   # #7: eigene Streifenfarben (leer = Akzent / Team-Farbe)
    # Feintuning
    "scale": 0,            # Tower-Skalierung (0 = automatisch einpassen)
    "rows": 0,             # nur Top-N Zeilen (0 = alle)
    "mapsize": 400,        # Trackmap-Kantenlänge in px
    "maprot": 110,         # Trackmap-Drehung in Grad
    "mapflip": True,       # Trackmap horizontal spiegeln
    "mapcorner": "tr",     # Trackmap-Position: tl/tc/tr/lc/rc/bl/bc/br (Ecken + Kantenmitten)
    "dotsize": 1.0,        # Größe der Auto-Punkte (Faktor)
    "holds": 300,          # Ergebnis nach Session-Ende halten (Sekunden)
    "ltdur": 4.0,          # Lower-Third Anzeigedauer (Sekunden)
    "flbdur": 4.5,         # Fastest-Lap-Banner Dauer (Sekunden)
    "dmgcrit": 60,         # Damage-Icon blinkt ab ... %
    "battlethresh": 1.5,   # Abstand (s), ab dem zwei Autos als "Battle" gelten
    "preset": "voll",
}

def _load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
    except Exception:
        return dict(DEFAULT_SETTINGS)

def _save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(overlay_settings, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[SETTINGS] Speichern fehlgeschlagen: {e}")

overlay_settings = _load_settings()

# Regie-Zustand: manuelle Einblendungen, gesteuert über /regie (zweites Gerät/Handy). Kommt im
# Payload mit; das Overlay spiegelt ihn (Verlaufs-Charts, Strategie, Head-to-Head, WM-Stand).
regie_state = {"chart": "none", "strat": False, "h2h": False, "champ": False}

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
# Runden-Gültigkeit je Auto: {idx: {"dirty": laufende Runde ungültig?, "valid": letzte Runde gültig?}}.
# m_currentLapInvalid (Track Limits) gilt für die LAUFENDE Runde -> wird über die Runde gesammelt,
# beim Rundenwechsel ausgewertet -> ungültige Runden zählen NICHT als Best-/schnellste Runde.
lap_track: dict[int, dict] = {}
last_packet_time = 0.0

# ── UDP-Recorder: rohe Pakete mitschneiden (für Replay in der Test-GUI) ──────────
# Format .f1rec = gzip: 1 JSON-Metazeile, danach Records [<d rel_ts][<I len][raw].
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
PARSED_PIDS = {0, 1, 2, 3, 4, 6, 7, 8, 10, 15, 16}   # nur was das Overlay verarbeitet
rec_lock = threading.Lock()
recorder = {"active": False, "fh": None, "t0": 0.0, "count": 0, "bytes": 0, "filter": None, "path": ""}

def _rec_start(name="", only_parsed=True):
    with rec_lock:
        if recorder["active"]:
            return {"ok": False, "err": "läuft bereits"}
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        safe = "".join(c for c in (name or "") if c.isalnum() or c in " _-")[:40].strip().replace(" ", "_")
        fn = time.strftime("%Y-%m-%d_%H%M%S") + (("_" + safe) if safe else "") + ".f1rec"
        path = os.path.join(RECORDINGS_DIR, fn)
        fh = gzip.open(path, "wb")
        meta = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "format": EXPECTED_FORMAT,
                "only_parsed": bool(only_parsed), "note": name or ""}
        fh.write((json.dumps(meta) + "\n").encode("utf-8"))
        recorder.update(active=True, fh=fh, t0=time.time(), count=0, bytes=0,
                        filter=(PARSED_PIDS if only_parsed else None), path=path)
        return {"ok": True, "file": fn}

def _rec_stop():
    with rec_lock:
        if not recorder["active"]:
            return {"ok": False, "err": "läuft nicht"}
        try:
            recorder["fh"].flush(); recorder["fh"].close()
        except Exception:
            pass
        info = {"ok": True, "file": os.path.basename(recorder["path"]),
                "count": recorder["count"], "mb": round(recorder["bytes"] / 1e6, 2)}
        recorder.update(active=False, fh=None)
        return info
# Car-Indizes, die laut Participants-Paket ein ECHTES Team tragen. Unbelegte Slots
# markiert das Spiel mit team_id 255/65535 -> die fliegen raus. Index-genau (nicht
# über m_numActiveCars), damit Fahrer auf hohen Indizes in Online-Lobbys drinbleiben.
valid_participants: set = set()
INVALID_TEAM_IDS = {255, 65535}
_last_session_uid = None   # erkennt Session-Wechsel -> Runden/FL/Fahrer zurücksetzen
_player_idx = 0            # Header playerCarIndex -> "auf wen bin ich drauf" (wenn nicht zuschauend)
race_control: list = []     # Rennleitungs-Meldungen (Strafen, MOM/Override, ...) fürs Overlay
_rc_id = 0
PENALTY_LABELS = {0: "Drive Through Penalty", 1: "Stop-&-Go-Strafe", 2: "Startplatzstrafe",
                  4: "Zeitstrafe", 5: "Verwarnung", 6: "Disqualifiziert", 9: "Reifen-Regel"}

last_quali: dict[str, float] = {}    # Bestzeiten der letzten Quali {Name: Sekunden} -> Grid-Screen
car_pos: dict[int, tuple] = {}       # Motion (Packet 0): Weltposition (x, z) je Auto -> Trackmap
final_classification: list = []      # Endergebnis (Packet 8) nach der Zielflagge
lap_positions: dict[int, dict] = {}  # Packet 15: {Runde: {car_idx: position}} -> Positionsverlauf-Chart
start_lights = {"num": 0, "out": False, "t": 0.0}   # STLG/LGOT-Events -> Start-Ampel
# Streckenkontur für die Trackmap: wird live aus EINER Runde des Führenden gelernt.
# Aufzeichnung startet sofort, sobald das Referenzauto echt auf der Strecke ist (nicht
# in der Box, keine Out-Lap) -> der Startpunkt liegt garantiert auf der Ideallinie und
# das Auto kommt nach einer Runde dorthin zurück -> saubere, geschlossene Schleife,
# Boxengasse nie dabei.
track_outline = {"pts": [], "done": False, "ver": 0}
_outline_ref = {"idx": None, "start_pt": None, "last": None, "dist": 0.0, "start_prog": None}

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
    "track_length": 0,
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
    "is_spectating": False,
    "spectator_index": 255,
    "marshal_zones": [],
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
        drivers[idx] = {"index": idx, "name": "", "team": "", "team_id": -1, "position": 0, "gap_to_leader": 0.0, "gap_to_ahead": 0.0, "compound": "?", "tyre_age": 0, "last_lap": 0.0, "current_lap_time": 0.0, "sector1": 0.0, "sector2": 0.0, "drs": False, "ers_pct": 0.0, "ers_mode": 0, "in_pit": False, "dnf": False, "dsq": False, "race_number": 0, "best_lap": 0.0, "driver_status": 0, "pit_time": 0.0, "overtake_active": False, "overtake_available": False,
                       "lap_invalid": False, "penalties": 0, "pen_dt": 0, "corner_warnings": 0, "pit_stops": 0, "stints": [],
                       "lap_num": 0, "lap_distance": 0.0, "laps_down": 0, "sector": 0, "grid_position": 0, "speed": 0, "throttle": 0.0, "brake": 0.0, "gear": 0, "dmg_fl": 0, "dmg_fr": 0, "dmg_rw": 0}
    return drivers[idx]

def parse_header(data):
    if len(data) < HEADER_SIZE:
        return None
    h = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    return {"format": h[0], "packet_id": h[5], "session_uid": h[6], "player_index": h[10]}

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
    track_length = struct.unpack_from('<H', data, base + 4)[0]   # m_trackLength (uint16, Meter)
    session_type = data[base + 6]
    track_id     = struct.unpack_from('<b', data, base + 7)[0]   # m_trackId (int8)
    formula      = data[base + 8]
    _tl_raw = struct.unpack_from('<H', data, base + 9)[0] if len(data) >= base + 11 else 0
    time_left = _tl_raw if _tl_raw <= 3600 else 0
    # m_safetyCarStatus (F1 26): 19 feste Bytes (inkl. uint16 m_trackLength) + 21 MarshalZones
    # (je 5 Bytes = 105) = Offset 124. Unverändert ggü. F1 25 (Marshal-Zones sind nicht
    # an die Autoanzahl gekoppelt).
    safety_car = data[base + 124]
    is_spectating = data[base + 15]      # m_isSpectating (1 = Zuschauer-Kamera)
    spectator_idx = data[base + 16]      # m_spectatorCarIndex (welches Auto die Kamera zeigt)

    # Marshal-Zonen: Anzahl @ +18, danach 21 Zonen à 5 Bytes (float zoneStart 0..1 der
    # Runde + int8 zoneFlag: 0=keine, 1=grün, 2=blau, 3=gelb). -> gelbe Abschnitte
    # auf der Trackmap einfärben.
    marshal_zones = []
    if len(data) > base + 19:
        num_mz = data[base + 18]
        for k in range(min(num_mz, 21)):
            off = base + 19 + k * 5
            if len(data) < off + 5:
                break
            zs = struct.unpack_from('<f', data, off)[0]
            zf = struct.unpack_from('<b', data, off + 4)[0]
            if 0.0 <= zs <= 1.0:
                marshal_zones.append({"s": round(zs, 4), "f": zf})

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

    # Streckenwechsel -> gelernte Trackmap-Kontur verwerfen. Session-Wechsel auf
    # derselben Strecke (z.B. Quali -> Rennen) behält sie.
    if session_info.get("track_id") != track_id:
        track_outline["pts"] = []
        track_outline["done"] = False
        track_outline["ver"] += 1
        _outline_ref.update({"idx": None, "start_pt": None, "last": None, "dist": 0.0, "start_prog": None})

    session_info.update({
        "track_id": track_id,
        "track_length": track_length,
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
        "is_spectating": bool(is_spectating),
        "spectator_index": spectator_idx,
        "marshal_zones": marshal_zones,
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
            cur_lap_ms  = struct.unpack_from('<I', data, start + 4)[0]   # m_currentLapTimeInMS (tickt hoch)
            s1_ms = struct.unpack_from('<H', data, start + 8)[0]
            s1_min = data[start + 10]
            s2_ms = struct.unpack_from('<H', data, start + 11)[0]
            s2_min = data[start + 13]
            gap_front  = struct.unpack_from('<H', data, start + 14)[0]
            gap_front_m = data[start + 16]
            gap_leader  = struct.unpack_from('<H', data, start + 17)[0]
            gap_leader_m = data[start + 19]
            lap_distance = struct.unpack_from('<f', data, start + 20)[0]   # m_lapDistance (Meter)
            position = data[start + 32]
            cur_sector = data[start + 36]      # m_sector: 0=S1, 1=S2, 2=S3
            grid_pos   = data[start + 43]      # m_gridPosition (Startplatz)
            pit_status = data[start + 34]
            num_pit_stops = data[start + 35]   # m_numPitStops
            lap_invalid = data[start + 37]     # m_currentLapInvalid (0=gültig, 1=ungültig)
            penalties_s = data[start + 38]     # m_penalties (aufaddierte Zeitstrafe in Sekunden)
            dt_pens      = data[start + 41]     # m_numUnservedDriveThroughPens (offene Durchfahrtstrafen)
            corner_warns = data[start + 40]     # m_cornerCuttingWarnings (Track-Limits-Verwarnungen)
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
        # Gültigkeit der Runde tracken: m_currentLapInvalid gilt für die LAUFENDE Runde und
        # wird an der Start/Ziel-Linie zurückgesetzt. Flag über die Runde sammeln ("sticky"),
        # beim Rundenwechsel für die GERADE beendete Runde festhalten.
        lap_num_now = data[start + 33]
        lt = lap_track.setdefault(i, {"dirty": False, "valid": True})
        if lap_num_now > d["lap_num"]:                 # neue Runde -> alte ist fertig
            lt["valid"] = not lt["dirty"]
            lt["dirty"] = False
        if lap_invalid == 1:                           # laufende Runde ungültig -> merken
            lt["dirty"] = True

        if 30000 < last_lap_ms < 600000:
            d["last_lap"] = last_lap_ms / 1000.0
            lap_s = last_lap_ms / 1000.0
            # Ungültige Runden (Track Limits) zählen NICHT als Best-/schnellste Runde.
            if lt["valid"]:
                if (session_info["fastest_lap_time"] == 0.0 or lap_s < session_info["fastest_lap_time"]) and d["name"]:
                    session_info["fastest_lap_time"] = lap_s
                    session_info["fastest_lap_driver"] = d["name"]
                if d["best_lap"] == 0.0 or lap_s < d["best_lap"]:   # Bestrunde des Fahrers (Quali)
                    d["best_lap"] = lap_s
        d["current_lap_time"] = cur_lap_ms / 1000.0 if 0 < cur_lap_ms < 600000 else 0.0
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
        d["penalties"] = penalties_s
        d["pen_dt"] = dt_pens
        d["corner_warnings"] = corner_warns
        d["dnf"] = result_stat in (4, 6, 7)
        d["dsq"] = result_stat == 5
        d["finished"] = result_stat == 3    # Zielflagge: Rennen beendet / Quali-Session durch
        d["driver_status"] = driver_status
        d["lap_distance"] = lap_distance    # für die Überrundungs-Erkennung (Fortschritt)
        d["sector"] = cur_sector if cur_sector in (0, 1, 2) else 0
        d["grid_position"] = grid_pos       # Startplatz (Comeback-Badge / Grid-Referenz)
        if pit_time_ms > 0:                 # Standzeit des letzten Boxenstopps (sticky)
            d["pit_time"] = pit_time_ms / 1000.0

        # Fallback-Name falls noch keiner gesetzt (z.B. Lap-Daten vor Participants-Paket)
        if not d["name"] and position > 0:
            team = TEAM_NAMES.get(d.get("team_id", -1), d.get("team", ""))
            formula_prefix = FORMULA_NAMES.get(session_info.get("formula", 0), "F1")
            d["name"] = build_fallback_name(team, d.get("race_number", 0), formula_prefix)

        if position > 0:
            lap_num = data[start + 33]
            d["lap_num"] = lap_num
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
            speed      = struct.unpack_from('<H', data, start + 0)[0]   # km/h
            throttle   = struct.unpack_from('<f', data, start + 2)[0]   # 0..1
            brake      = struct.unpack_from('<f', data, start + 10)[0]  # 0..1
            gear       = struct.unpack_from('<b', data, start + 15)[0]  # -1=R, 0=N, 1-8
            drs_status = data[start + 18]
        except (struct.error, IndexError):
            continue
        d = get_driver(i)
        d["drs"]      = drs_status == 1
        d["speed"]    = speed
        d["throttle"] = round(min(max(throttle, 0.0), 1.0), 2)
        d["brake"]    = round(min(max(brake, 0.0), 1.0), 2)
        d["gear"]     = gear

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

# Packet 0: Motion – Weltposition aller Autos (für die Trackmap). CarMotionData = 54 Bytes
# (9 floats: pos3+vel3+ypr3, + 9 int16: dir6+gforce3 -> 36+18=54; Paket 29+24*54=1325).
# worldPositionX float @0, worldPositionZ float @8.
MOTION_SIZE = 54

def handle_motion(data):
    base = HEADER_SIZE
    for i in range(MAX_CARS):
        start = base + i * MOTION_SIZE
        if start + 12 > len(data):
            break
        x = struct.unpack_from('<f', data, start + 0)[0]
        z = struct.unpack_from('<f', data, start + 8)[0]
        # Leere/uninitialisierte Slots senden Müll (±1e38, NaN) -> verwerfen, sonst
        # fliegen Punkte aus der Karte und die Kontur wird vergiftet. F1-Strecken
        # liegen im Bereich weniger tausend Meter um den Ursprung.
        if -1e5 < x < 1e5 and -1e5 < z < 1e5:
            car_pos[i] = (round(x, 1), round(z, 1))
    _learn_outline()

def _learn_outline():
    """Streckenkontur aus EINER Runde des Führenden lernen. Startet sofort, sobald das
    Referenzauto echt auf der Strecke fährt (driver_status 1/4, nicht in der Box) ->
    der Startpunkt liegt auf der Ideallinie. Alle ~5 m ein Punkt; sobald das Auto nach
    ~einer Runde (>1 km gefahren) wieder < 30 m am Startpunkt ist, schließt die Schleife.
    Referenzauto wird gehalten, bis es unbrauchbar wird (Box/DNF/weg)."""
    if track_outline["done"]:
        return
    o = _outline_ref
    tl = session_info.get("track_length", 0)
    idx = o["idx"]
    # Hinweis: m_driverStatus ist im Rennen unbrauchbar (das Spiel meldet für alle Autos
    # 2 = "in lap"), daher NUR über in_pit/Position filtern. Die Boxengasse fällt über
    # m_pitStatus (in_pit) raus.
    ref_ok = (idx is not None and idx in car_pos and idx in drivers
              and not drivers[idx]["in_pit"] and not drivers[idx]["dnf"] and not drivers[idx]["dsq"]
              and not (o["start_prog"] is None and tl > 0))   # Streckenlänge kam nach -> neu locken (Fortschritt)
    if not ref_ok:
        # (Neu-)Wahl der Referenz: bestplatziertes Auto auf der Strecke -> sofort mit dem
        # Aufzeichnen beginnen (Startpunkt = echter Streckenpunkt). Start-Fortschritt merken.
        o.update({"idx": None, "start_pt": None, "last": None, "dist": 0.0, "start_prog": None})
        track_outline["pts"] = []
        for d in sorted(drivers.values(), key=lambda x: x["position"] or 99):
            if d["position"] > 0 and d["index"] in car_pos and not d["in_pit"]:
                x, z = car_pos[d["index"]]
                sp = (d["lap_num"] + max(0.0, d["lap_distance"]) / tl) if tl > 0 else None
                o.update({"idx": d["index"], "start_pt": (x, z), "last": (x, z), "dist": 0.0, "start_prog": sp})
                frac = round(max(0.0, d["lap_distance"]) / tl, 4) if tl > 0 else -1
                track_outline["pts"] = [[x, z, frac, d.get("sector", 0)]]
                break
        return
    d = drivers[idx]
    x, z = car_pos[idx]
    lx, lz = o["last"]
    step2 = (x - lx) ** 2 + (z - lz) ** 2
    if step2 >= 25.0:                                 # >= 5 m seit dem letzten Punkt
        # Punkt = [x, z, Rundenanteil 0..1, Sektor 0..2] -> Frontend kann Sektoren
        # einfärben und Marshal-Zonen (Flaggen) auf die Kontur mappen.
        frac = round(max(0.0, d["lap_distance"]) / tl, 4) if tl > 0 else -1
        track_outline["pts"].append([x, z, frac, d.get("sector", 0)])
        o["dist"] += step2 ** 0.5
        o["last"] = (x, z)
    # Schließen: GENAU eine volle Runde. Bevorzugt exakt über den Fortschritt
    # (Runde + lap_distance/Streckenlänge) -> kein Fehl-Schließen bei engen/parallelen
    # Streckenpassagen. Ohne Streckenlänge Fallback über Distanz + Nähe zum Start.
    closed = False
    if tl > 0 and o["start_prog"] is not None:
        prog = d["lap_num"] + max(0.0, d["lap_distance"]) / tl
        if prog - o["start_prog"] >= 1.0 and len(track_outline["pts"]) > 60:
            closed = True
    else:
        sx, sz = o["start_pt"]
        if o["dist"] > 1000.0 and (x - sx) ** 2 + (z - sz) ** 2 < 900.0:
            closed = True
    if closed or len(track_outline["pts"]) > 4000:   # 4000 = Sicherheitsnetz
        track_outline["done"] = True
        track_outline["ver"] += 1

# Packet 8: Final Classification – offizielles Endergebnis nach der Zielflagge.
# FinalClassificationData = 46 Bytes: pos/laps/grid/points/stops/status/reason (7x uint8),
# bestLapMS uint32 @7, totalRaceTime double @11, penaltiesTime @19, numPenalties @20,
# numTyreStints @21, stintsActual @22..29, stintsVisual @30..37, stintsEndLaps @38..45.
FINAL_CLASS_SIZE = 46

def handle_final_classification(data):
    base = HEADER_SIZE
    if len(data) < base + 1:
        return
    num = data[base]
    results = []
    for i in range(min(num, MAX_CARS)):
        start = base + 1 + i * FINAL_CLASS_SIZE
        if start + FINAL_CLASS_SIZE > len(data):
            break
        pos, laps, grid, points, stops, status, reason = data[start:start + 7]
        # 0=invalid, 1=inactive -> kein echtes Ergebnis
        if status in (0, 1) or pos == 0:
            continue
        best_ms  = struct.unpack_from('<I', data, start + 7)[0]
        total_s  = struct.unpack_from('<d', data, start + 11)[0]
        pen_s    = data[start + 19]
        n_stints = data[start + 21]
        stints   = [COMPOUND.get(data[start + 30 + k], "?") for k in range(min(n_stints, 8))]
        d = drivers.get(i, {})
        results.append({
            "position": pos, "index": i,
            "name": d.get("name") or f"Auto #{i}",
            "team": d.get("team", ""),
            "grid": grid, "points": points, "stops": stops, "laps": laps,
            "status": status,                 # 3=fertig, 4=DNF, 5=DSQ, 6=NC, 7=retired
            "best_lap": best_ms / 1000.0,
            "total_time": total_s + pen_s,    # inkl. Zeitstrafen
            "penalties": pen_s,
            "stints": stints,
        })
    if results:
        results.sort(key=lambda r: r["position"])
        final_classification.clear()
        final_classification.extend(results)

# Packet 10: Car Damage – hier nur der Flügelschaden fürs Overlay (kleine Icons).
# CarDamageData = 46 Bytes: tyresWear 4xfloat @0, tyresDamage @16, brakesDamage @20,
# tyreBlisters @24, FL-Flügel @28, FR-Flügel @29, Heckflügel @30.
DAMAGE_SIZE = 46

def handle_car_damage(data):
    base = HEADER_SIZE
    for i in range(MAX_CARS):
        start = base + i * DAMAGE_SIZE
        if start + DAMAGE_SIZE > len(data):
            break
        d = get_driver(i)
        d["dmg_fl"] = data[start + 28]   # Frontflügel links
        d["dmg_fr"] = data[start + 29]   # Frontflügel rechts
        d["dmg_rw"] = data[start + 30]   # Heckflügel

# Packet 15: Lap Positions – Position jedes Autos zu Beginn jeder Runde (fürs Chart).
def handle_lap_positions(data):
    base = HEADER_SIZE
    if len(data) < base + 2:
        return
    num_laps  = data[base]
    lap_start = data[base + 1]
    grid = base + 2
    for L in range(min(num_laps, 50)):
        row = grid + L * MAX_CARS
        if row + MAX_CARS > len(data):
            break
        entry = {}
        for c in range(MAX_CARS):
            p = data[row + c]
            if p > 0:
                entry[c] = p
        if entry:
            lap_positions[lap_start + L + 1] = entry   # 1-basierte Rundennummer

def _car_name(veh):
    return (drivers.get(veh) or {}).get("name") or f"Auto #{veh}"

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
    b = HEADER_SIZE + 4
    if code == "PENA" and len(data) >= HEADER_SIZE + 11:
        pen_type, veh, pen_time = data[b], data[b + 2], data[b + 4]
        name = _car_name(veh)
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
    elif code == "FTLP" and len(data) >= HEADER_SIZE + 9:
        # Offizielles Schnellste-Runde-Event: vehicleIdx uint8, lapTime float (Sekunden)
        veh = data[b]
        lap_time = struct.unpack_from('<f', data, b + 1)[0]
        name = _car_name(veh)
        if 0 < lap_time < 600:
            session_info["fastest_lap_time"] = round(lap_time, 3)
            session_info["fastest_lap_driver"] = name
            mm, ss = int(lap_time // 60), lap_time % 60
            push_rc("fl", f"{name}: Schnellste Runde {mm}:{ss:06.3f}")
    elif code == "RTMT" and len(data) >= HEADER_SIZE + 5:
        push_rc("retire", f"{_car_name(data[b])}: Aufgabe (DNF)")
    elif code == "RDFL":
        push_rc("redflag", "ROTE FLAGGE")
    elif code == "CHQF":
        push_rc("flag", "Zielflagge")
    elif code == "RCWN" and len(data) >= HEADER_SIZE + 5:
        push_rc("winner", f"{_car_name(data[b])} gewinnt das Rennen!")
    elif code == "DTSV" and len(data) >= HEADER_SIZE + 5:
        push_rc("penserved", f"{_car_name(data[b])}: Drive Through Penalty abgeleistet")
    elif code == "SGSV" and len(data) >= HEADER_SIZE + 5:
        push_rc("penserved", f"{_car_name(data[b])}: Stop-&-Go-Strafe abgeleistet")
    elif code == "STLG" and len(data) >= HEADER_SIZE + 5:
        # Start-Ampel: kommt pro aufleuchtendem Licht (num = wie viele an sind)
        start_lights["num"] = data[b]
        start_lights["out"] = False
        start_lights["t"] = time.time()
    elif code == "LGOT":
        start_lights["out"] = True
        start_lights["t"] = time.time()
    elif code == "SSTA":
        # Session-Start: Ampel zurücksetzen, altes Endergebnis ausblenden
        start_lights.update({"num": 0, "out": False, "t": 0.0})
        final_classification.clear()

_debug_count = 0
_warned_format = None

def udp_listener():
    global last_packet_time, _debug_count, _warned_format, _last_session_uid, _player_idx
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
            # Recorder: rohe Pakete wegschreiben (eigener Lock, hält state_lock nicht auf)
            if recorder["active"] and (recorder["filter"] is None or pid in recorder["filter"]):
                with rec_lock:
                    if recorder["fh"]:
                        try:
                            recorder["fh"].write(struct.pack('<dI', time.time() - recorder["t0"], len(data)) + data)
                            recorder["count"] += 1; recorder["bytes"] += len(data)
                        except Exception:
                            pass
            if DEBUG:
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
                    # War die alte Session eine Quali? -> Bestzeiten fürs Grid-Screen retten.
                    if session_info.get("is_quali"):
                        last_quali.clear()
                        for d in drivers.values():
                            if d["best_lap"] > 0 and d["name"]:
                                last_quali[d["name"]] = d["best_lap"]
                    _last_session_uid = h["session_uid"]
                    drivers.clear()
                    lap_track.clear()
                    valid_participants.clear()
                    session_info["current_lap"]        = 0
                    session_info["fastest_lap_time"]   = 0.0
                    session_info["fastest_lap_driver"] = ""
                    race_control.clear()
                    car_pos.clear()
                    final_classification.clear()
                    lap_positions.clear()
                    start_lights.update({"num": 0, "out": False, "t": 0.0})
                    # Neue Session -> Trackmap neu lernen (nicht die alte/Test-Kontur zeigen);
                    # die Karte kommt erst wieder, wenn sie ~1 Runde gelernt wurde.
                    track_outline["pts"] = []
                    track_outline["done"] = False
                    track_outline["ver"] += 1
                    _outline_ref.update({"idx": None, "start_pt": None, "last": None, "dist": 0.0, "start_prog": None})
                last_packet_time = time.time()
                _player_idx = h.get("player_index", _player_idx)
                if pid == 1:
                    handle_session(data)
                    if DEBUG:
                        print(f"[P1] Formula={session_info['formula_name']} SC={session_info['safety_car_status']}")
                elif pid == 4:
                    handle_participants(data)
                    if DEBUG:
                        names = [d["name"] for d in drivers.values() if d["name"]]
                        print(f"[P4] {len(names)} Namen: {names[:3]}")
                elif pid == 2:
                    handle_lap_data(data)
                    if DEBUG:
                        active = [d for d in drivers.values() if d["position"] > 0]
                        print(f"[P2] {len(active)} aktive Fahrer")
                elif pid == 7:
                    handle_car_status(data)
                elif pid == 6:
                    handle_car_telemetry(data)
                elif pid == 16:
                    handle_car_telemetry2(data)   # 2026: Overtake Mode (DRS-Ersatz)
                elif pid == 3:
                    handle_event(data)
                elif pid == 0:
                    handle_motion(data)            # Weltpositionen -> Trackmap
                elif pid == 8:
                    handle_final_classification(data)
                elif pid == 10:
                    handle_car_damage(data)
                elif pid == 15:
                    handle_lap_positions(data)
        except Exception as e:
            print(f"[UDP] Error: {e}")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/test")
def index_test():
    # Test-Overlay (Kopie von index.html, hier wird Neues ausprobiert) -> /test
    return render_template("test.html")

@app.route("/regie")
def regie_page():
    # Regie-Steuerung (zweites Gerät): blendet manuelle Panels im Overlay ein/aus.
    return render_template("regie.html")

@app.route("/api/regie", methods=["GET", "POST"])
def api_regie():
    global regie_state
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        with state_lock:
            if d.get("chart") in ("none", "pos", "gap", "lap"):
                regie_state["chart"] = d["chart"]
            for k in ("strat", "h2h", "champ"):
                if k in d:
                    regie_state[k] = bool(d[k])
    with state_lock:
        return jsonify(dict(regie_state))

def calculate_championship():
    # A3: WM-Stand aus championship.json + Live-Positionen (Logik übernommen aus KERS_WM_Overlay):
    # Gesamtpunkte = base_points + Punkte für die aktuelle Rennposition (nur Top 10). Der Namens-
    # abgleich läuft über in_game_name -> name -> Teilstring, damit die Anzeige echte Namen zeigen
    # kann, während telemetrisch der Spiel-Handle ankommt. Live-Positionen aus dem Overlay-Zustand.
    try:
        with open(CHAMP_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {"title": "", "standings": []}
    with state_lock:
        live = {}
        for d in drivers.values():
            nm = (d.get("name") or "").strip()
            if nm and d.get("position", 0) > 0:
                live[nm] = d["position"]
    standings = []
    for drv in cfg.get("drivers", []):
        name = drv.get("name", "")
        base = drv.get("base_points", 0)
        ign = drv.get("in_game_name") or name
        pos = live.get(ign)
        if pos is None:
            pos = live.get(name)
        if pos is None:
            for tele_name, tele_pos in live.items():
                if name and name.lower() in tele_name.lower():
                    pos = tele_pos
                    break
        live_pts = POINTS_SYSTEM.get(pos, 0) if pos and 1 <= pos <= 10 else 0
        standings.append({"name": name, "team": drv.get("team", ""), "base_points": base,
                          "live_points": live_pts, "total_points": base + live_pts, "live_position": pos})
    standings.sort(key=lambda x: (x["total_points"], x["base_points"]), reverse=True)
    return {"title": cfg.get("title", "Championship"), "standings": standings}

@app.route("/api/championship")
def api_championship():
    return jsonify(calculate_championship())

def build_live_payload():
    """Kompletter Live-Zustand fürs Overlay. Muss unter state_lock laufen — alle
    veränderlichen Container werden hier kopiert, damit jsonify/json.dumps nach dem
    Lock-Release nicht mit dem UDP-Thread kollidiert."""
    formula_prefix = FORMULA_NAMES.get(session_info.get("formula", 0), "F1")
    active = []
    for d in drivers.values():
        if d["position"] > 0 and (not valid_participants or d["index"] in valid_participants):
            entry = dict(d)
            if not entry["name"]:
                team = TEAM_NAMES.get(entry.get("team_id", -1), entry.get("team", ""))
                entry["name"] = build_fallback_name(team, entry.get("race_number", 0), formula_prefix)
            p = car_pos.get(d["index"])
            if p:
                entry["pos_xz"] = p            # Weltposition für die Trackmap
            active.append(entry)
    active.sort(key=lambda x: x["position"])
    # Überrundungen: Fortschritt = Runde + Rundenanteil (lap_distance/Streckenlänge).
    # laps_down = ganze Runden, die der Führende voraus ist. Aus dem KONTINUIERLICHEN
    # Fortschritt gerechnet -> kein Flackern an der Start/Ziel-Linie (ein Nachzügler auf
    # der Führungsrunde wird korrekt NICHT markiert). Nur im Rennen sinnvoll.
    tl = session_info.get("track_length", 0)
    if tl > 0 and not session_info.get("is_quali") and active:
        def _prog(e):
            return e.get("lap_num", 0) + max(0.0, e.get("lap_distance", 0.0)) / tl
        lead_prog = max(_prog(e) for e in active)
        for e in active:
            e["laps_down"] = max(0, int(lead_prog - _prog(e)))
    else:
        for e in active:
            e["laps_down"] = 0
    connected = (time.time() - last_packet_time) < 3.0
    # "Auf wen bin ich drauf": beim Zuschauen das beobachtete Auto, sonst das eigene.
    spec = session_info.get("spectator_index", 255)
    focus_index = spec if (session_info.get("is_spectating") and spec != 255) else _player_idx
    sl_age = round(time.time() - start_lights["t"], 2) if start_lights["t"] else 9999
    return {"drivers": active, "connected": connected, "session": dict(session_info),
            "race_control": list(race_control), "focus_index": focus_index,
            "start_lights": {"num": start_lights["num"], "out": start_lights["out"], "age": sl_age},
            "final_classification": [dict(r) for r in final_classification],
            "quali_results": dict(last_quali),
            "settings": dict(overlay_settings),
            "regie": dict(regie_state)}

@app.route("/api/live")
def api_live():
    with state_lock:
        payload = build_live_payload()
    return jsonify(payload)

@app.route("/api/stream")
def api_stream():
    # Server-Sent Events: pusht den Live-Zustand sobald neue UDP-Daten da sind
    # (~8x/s), sonst 1x/s als Heartbeat. Frontend fällt bei Fehlern auf Polling zurück.
    def gen():
        last_sent = 0.0
        while True:
            # 80 ms Takt: Kamerawechsel (Spectator-Index) so schnell wie möglich
            # durchreichen. Harte Untergrenze bleibt das Spiel selbst (Session-Paket 2x/s).
            time.sleep(0.08)
            now = time.time()
            with state_lock:
                fresh = last_packet_time > last_sent
                if not fresh and now - last_sent < 1.0:
                    continue
                payload = build_live_payload()
            last_sent = now
            yield "data: " + json.dumps(payload) + "\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/track")
def api_track():
    # Gelernte Streckenkontur (Punktliste). ver ändert sich bei Reset/Fertigstellung ->
    # Frontend pollt bis done und zeichnet nur bei neuer Version neu.
    with state_lock:
        o = _outline_ref
        tl = session_info.get("track_length", 0)
        cur_prog = None
        if o["idx"] is not None and o["idx"] in drivers and tl > 0:
            dd = drivers[o["idx"]]
            cur_prog = round(dd["lap_num"] + max(0.0, dd["lap_distance"]) / tl, 3)
        return jsonify({"pts": [list(p) for p in track_outline["pts"]],
                        "done": track_outline["done"], "ver": track_outline["ver"],
                        "_dbg": {"ref_idx": o["idx"], "start_prog": o["start_prog"],
                                 "cur_prog": cur_prog, "dist_m": round(o["dist"]), "tl": tl}})

@app.route("/api/track/relearn", methods=["POST", "GET"])
def api_track_relearn():
    # Kontur verwerfen und neu lernen (falls eine verzerrte Runde erwischt wurde).
    with state_lock:
        track_outline["pts"] = []
        track_outline["done"] = False
        track_outline["ver"] += 1
        _outline_ref.update({"idx": None, "start_pt": None, "last": None, "dist": 0.0, "start_prog": None})
    return jsonify({"ok": True})

@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    d = request.get_json(silent=True) or {}
    return jsonify(_rec_start(d.get("name", ""), bool(d.get("only_parsed", True))))

@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    return jsonify(_rec_stop())

@app.route("/api/record/status")
def api_record_status():
    with rec_lock:
        return jsonify({"active": recorder["active"],
                        "file": os.path.basename(recorder["path"]) if recorder["path"] else "",
                        "secs": round(time.time() - recorder["t0"], 1) if recorder["active"] else 0,
                        "count": recorder["count"], "mb": round(recorder["bytes"] / 1e6, 2)})

@app.route("/api/lap_positions")
def api_lap_positions():
    # Positionsverlauf (Packet 15) + Fahrer-Metadaten für Legende/Farben
    with state_lock:
        meta = {str(i): {"name": d["name"], "team": d["team"]}
                for i, d in drivers.items()
                if not valid_participants or i in valid_participants}
        return jsonify({"laps": {str(k): dict(v) for k, v in lap_positions.items()},
                        "drivers": meta})

@app.route("/api/status")
def api_status():
    connected = (time.time() - last_packet_time) < 3.0
    return jsonify({"connected": connected, "driver_count": len(drivers)})

@app.route("/settings")
def settings_page():
    # Einstellungs-Oberfläche: vom PC ODER z.B. Laptop/Handy im Heimnetz aufrufbar
    # (http://<PC-IP>:5100/settings) -> Änderungen wirken live im Overlay.
    return render_template("settings.html")

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    global overlay_settings
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with state_lock:
            for k, v in data.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                dv = DEFAULT_SETTINGS[k]
                try:
                    if isinstance(dv, bool):
                        overlay_settings[k] = bool(v)
                    elif isinstance(dv, (int, float)):
                        overlay_settings[k] = type(dv)(v) if not isinstance(v, bool) else dv
                    else:
                        overlay_settings[k] = str(v)
                except (TypeError, ValueError):
                    pass
            _save_settings()
    with state_lock:
        return jsonify(dict(overlay_settings))

@app.route("/api/settings/reset", methods=["POST"])
def api_settings_reset():
    global overlay_settings
    with state_lock:
        overlay_settings = dict(DEFAULT_SETTINGS)
        _save_settings()
        return jsonify(dict(overlay_settings))

if __name__ == "__main__":
    t = threading.Thread(target=udp_listener, daemon=True)
    t.start()
    webbrowser.open("http://127.0.0.1:5100")
    # threaded=True ist Pflicht: der SSE-Endpoint (/api/stream) hält pro Browser eine
    # Verbindung dauerhaft offen. Ohne Threading würde der eine Worker blockieren und
    # keine weiteren Requests (Overlay, /api/live, static) mehr beantworten.
    app.run(host="0.0.0.0", port=5100, debug=False, use_reloader=False, threaded=True)