# Patch: Debug-Version — einfach über main.py drüberkopieren zum testen

import json, os, socket, struct, threading, time, webbrowser
from flask import Flask, render_template, jsonify

app = Flask(__name__)

UDP_IP   = "0.0.0.0"
UDP_PORT = 20777

HEADER_FORMAT = '<HBBBBBQfIIBB'
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)

COMPOUND = {0: "W", 1: "I", 2: "E", 16: "S", 17: "M", 18: "H", 7: "I", 8: "W"}
COMPOUND_COLOR = {"S": "#FF3333", "M": "#FFDD00", "H": "#FFFFFF", "I": "#39CFFF", "W": "#4499FF", "E": "#FF9900"}
DRS_ACTIVE = {10, 12, 14}
state_lock = threading.Lock()
drivers: dict[int, dict] = {}
last_packet_time = 0.0

WEATHER = {0: "Klar ☀️", 1: "Leicht bewölkt 🌤️", 2: "Bewölkt ☁️", 3: "Leichter Regen 🌦️", 4: "Starker Regen 🌧️", 5: "Gewitter ⛈️"}
SESSION_TYPE = {0: "Unbekannt", 1: "P1", 2: "P2", 3: "P3", 4: "Short P", 5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Q", 9: "OSQ", 10: "Rennen", 11: "Rennen 2", 12: "Rennen 3", 13: "Zeitrennen", 255: "Unbekannt"}
QUALI_TYPES = {5, 6, 7, 8, 9}
session_info = {"type": 0, "type_name": "Unbekannt", "weather": 0, "weather_name": "Klar ☀️", "track_temp": 0, "air_temp": 0, "total_laps": 0, "current_lap": 0, "fastest_lap_time": 0.0, "fastest_lap_driver": "", "is_quali": False, "time_left": 0}
TEAM_NAMES = {1: "Red Bull", 6: "Ferrari", 0: "Mercedes", 3: "McLaren", 4: "Aston Martin", 9: "Alpine", 5: "RB", 2: "Haas", 7: "Williams", 8: "Sauber"}

def get_driver(idx):
    if idx not in drivers:
        drivers[idx] = {"index": idx, "name": "", "team": "", "position": 0, "gap_to_leader": 0.0, "gap_to_ahead": 0.0, "compound": "?", "tyre_age": 0, "last_lap": 0.0, "sector1": 0.0, "sector2": 0.0, "drs": False, "ers_pct": 0.0, "in_pit": False, "dnf": False}
    return drivers[idx]

def parse_header(data):
    if len(data) < HEADER_SIZE:
        return None
    h = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    return {"format": h[0], "packet_id": h[5], "session_uid": h[6]}

PARTICIPANT_SIZE = 57
P4_NAME_OFFSET = 7
P4_TEAM_OFFSET = 3

def handle_session(data):
    base = HEADER_SIZE
    if len(data) < base + 13:
        return
    weather = data[base + 0]
    track_temp = data[base + 1]
    air_temp = data[base + 2]
    total_laps = data[base + 3]
    session_type = data[base + 6]
    time_left = struct.unpack_from('<H', data, base + 10)[0] if len(data) >= base + 12 else 0
    session_info.update({"type": session_type, "type_name": SESSION_TYPE.get(session_type, "Unbekannt"), "weather": weather, "weather_name": WEATHER.get(weather, "Klar ☀️"), "track_temp": track_temp, "air_temp": air_temp, "total_laps": total_laps, "is_quali": session_type in QUALI_TYPES, "time_left": time_left})

def handle_participants(data):
    base = HEADER_SIZE + 1
    for i in range(22):
        start = base + i * PARTICIPANT_SIZE
        if start + P4_NAME_OFFSET + 32 > len(data):
            break
        team_id = data[start + P4_TEAM_OFFSET]
        name_bytes = data[start + P4_NAME_OFFSET: start + P4_NAME_OFFSET + 32]
        name = name_bytes.decode("utf-8", "ignore").split("\x00", 1)[0]
        if name:
            d = get_driver(i)
            d["name"] = name
            d["team"] = TEAM_NAMES.get(team_id, f"T{team_id}")

LAP_DATA_SIZE = 57

def handle_lap_data(data):
    base = HEADER_SIZE
    for i in range(22):
        start = base + i * LAP_DATA_SIZE
        if start + LAP_DATA_SIZE > len(data):
            break
        try:
            last_lap_ms = struct.unpack_from('<I', data, start + 0)[0]
            s1_ms = struct.unpack_from('<H', data, start + 8)[0]
            s1_min = data[start + 10]
            s2_ms = struct.unpack_from('<H', data, start + 11)[0]
            s2_min = data[start + 13]
            gap_front = struct.unpack_from('<H', data, start + 14)[0]
            gap_front_m = data[start + 16]
            gap_leader = struct.unpack_from('<H', data, start + 17)[0]
            gap_leader_m = data[start + 19]
            position = data[start + 32]
            pit_status = data[start + 34]
            result_stat = data[start + 45]
        except struct.error:
            continue
        if position == 0:
            continue
        d = get_driver(i)
        if last_lap_ms > 0:
            d["last_lap"] = last_lap_ms / 1000.0
            lap_s = last_lap_ms / 1000.0
            if (session_info["fastest_lap_time"] == 0.0 or lap_s < session_info["fastest_lap_time"]) and d["name"]:
                session_info["fastest_lap_time"] = lap_s
                session_info["fastest_lap_driver"] = d["name"]
        d["sector1"] = s1_min * 60 + s1_ms / 1000.0
        d["sector2"] = s2_min * 60 + s2_ms / 1000.0
        d["gap_to_ahead"] = gap_front_m * 60 + gap_front / 1000.0
        d["gap_to_leader"] = gap_leader_m * 60 + gap_leader / 1000.0
        d["position"] = position
        d["in_pit"] = pit_status in (1, 2)
        d["dnf"] = result_stat in (4, 5, 6)
        if position > 0:
            lap_num = data[start + 33]
            if lap_num > session_info["current_lap"]:
                session_info["current_lap"] = lap_num

CAR_STATUS_SIZE = 55
ERS_MAX = 4_000_000.0

def handle_car_status(data):
    base = HEADER_SIZE
    for i in range(22):
        start = base + i * CAR_STATUS_SIZE
        if start + CAR_STATUS_SIZE > len(data):
            break
        try:
            visual_comp = data[start + 26]
            tyre_age = data[start + 27]
            ers_store = struct.unpack_from('<f', data, start + 37)[0]
        except (struct.error, IndexError):
            continue
        d = get_driver(i)
        d["compound"] = COMPOUND.get(visual_comp, "?")
        d["tyre_age"] = tyre_age
        d["ers_pct"] = round(min(max(ers_store / ERS_MAX * 100, 0), 100), 1)

CAR_TELEM_SIZE = 60

def handle_car_telemetry(data):
    base = HEADER_SIZE
    for i in range(22):
        start = base + i * CAR_TELEM_SIZE
        if start + 19 > len(data):
            break
        try:
            drs_status = data[start + 18]
        except IndexError:
            continue
        get_driver(i)["drs"] = drs_status == 1

_debug_count = 0

def udp_listener():
    global last_packet_time, _debug_count
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((UDP_IP, UDP_PORT))
        print(f"[UDP] Listening on :{UDP_PORT}")
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
            print(f"[PKT] pid={pid} len={len(data)}")
            with state_lock:
                last_packet_time = time.time()
                if pid == 1:
                    handle_session(data)
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
        except Exception as e:
            print(f"[UDP] Error: {e}")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/live")
def api_live():
    with state_lock:
        active = [d for d in drivers.values() if d["name"] and d["position"] > 0]
        active.sort(key=lambda x: x["position"])
        connected = (time.time() - last_packet_time) < 3.0
    return jsonify({"drivers": active, "connected": connected, "session": session_info})

@app.route("/api/status")
def api_status():
    connected = (time.time() - last_packet_time) < 3.0
    return jsonify({"connected": connected, "driver_count": len(drivers)})

if __name__ == "__main__":
    t = threading.Thread(target=udp_listener, daemon=True)
    t.start()
    
    webbrowser.open("http://127.0.0.1:5100")
    app.run(host="0.0.0.0", port=5100, debug=False, use_reloader=False)