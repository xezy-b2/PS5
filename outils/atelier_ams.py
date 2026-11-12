# Atelier Coque · pont AMS (Mac / Windows / Linux, Python 3 sans dépendance)
# Lit les filaments de l'AMS d'une imprimante Bambu Lab sur le réseau local (lecture seule)
# et les met à disposition du site sur http://127.0.0.1:8765/ams.
# L'imprimante reste en mode normal : Bambu Handy et le cloud continuent de fonctionner.
import json, os, socket, ssl, struct, sys, threading, time, random, getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
CONF = os.path.expanduser("~/.atelier-coque-ams.json")
ORIGINS = ("https://xezy-b2.github.io", "http://localhost", "http://127.0.0.1")
state = {"connected": False, "error": None, "ams": None, "vt": None, "updated": None, "model": None}
lock = threading.Lock()

def ask_config():
    print("\n=== Atelier Coque · configuration de l'imprimante ===")
    print("Sur l'écran de l'imprimante : Réglages → Réseau (adresse IP et code d'accès)")
    print("et Réglages → Appareil (numéro de série).\n")
    ip = input("Adresse IP de l'imprimante (ex. 192.168.1.42) : ").strip()
    serial = input("Numéro de série (ex. 22E8BJ...) : ").strip().upper()
    code = getpass.getpass("Code d'accès LAN (8 caractères, ne s'affiche pas) : ").strip()
    cfg = {"ip": ip, "serial": serial, "code": code}
    with open(CONF, "w") as f: json.dump(cfg, f)
    try: os.chmod(CONF, 0o600)
    except Exception: pass
    print("Configuration enregistrée dans", CONF, "\n")
    return cfg

def load_config():
    if "--reset" in sys.argv or not os.path.exists(CONF): return ask_config()
    with open(CONF) as f: return json.load(f)

# --- MQTT 3.1.1 minimal (CONNECT / SUBSCRIBE / PUBLISH / PINGREQ) sur TLS ---
def enc_str(s): b = s.encode(); return struct.pack("!H", len(b)) + b
def enc_len(n):
    out = b""
    while True:
        d = n % 128; n //= 128
        out += bytes([d | (0x80 if n else 0)])
        if not n: return out
def packet(t, body): return bytes([t]) + enc_len(len(body)) + body
def read_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c: raise ConnectionError("connexion fermée par l'imprimante")
        buf += c
    return buf
def read_packet(s):
    h = read_exact(s, 1)[0]; mult, n = 1, 0
    while True:
        d = read_exact(s, 1)[0]; n += (d & 127) * mult; mult *= 128
        if not d & 128: break
    return h, read_exact(s, n) if n else b""

def merge(report):
    p = report.get("print") or {}
    with lock:
        if "ams" in p and isinstance(p["ams"], dict) and "ams" in p["ams"]:
            # P1/P2 envoient des mises à jour partielles : on fusionne par AMS et par emplacement
            cur = {str(a.get("id")): a for a in (state["ams"] or [])}
            for a in p["ams"]["ams"]:
                old = cur.get(str(a.get("id")), {})
                trays = {str(t.get("id")): t for t in old.get("tray", [])}
                for t in a.get("tray", []): trays[str(t.get("id"))] = {**trays.get(str(t.get("id")), {}), **t}
                cur[str(a.get("id"))] = {**old, **a, "tray": list(trays.values())}
            state["ams"] = list(cur.values()); state["updated"] = time.time()
        if "vt_tray" in p: state["vt"] = p["vt_tray"]
        state["connected"] = True; state["error"] = None

def mqtt_loop(cfg):
    while True:
        try:
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE   # certificat de l'imprimante auto-signé
            raw = socket.create_connection((cfg["ip"], 8883), timeout=10)
            s = ctx.wrap_socket(raw, server_hostname=cfg["ip"])
            cid = "atelier-coque-%06d" % random.randint(0, 999999)
            body = enc_str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60) + enc_str(cid) + enc_str("bblp") + enc_str(cfg["code"])
            s.sendall(packet(0x10, body))
            h, b = read_packet(s)
            if h >> 4 != 2 or len(b) < 2 or b[1] != 0:
                raise PermissionError("connexion refusée (code d'accès ou numéro de série incorrect ?)")
            topic = "device/%s/report" % cfg["serial"]
            s.sendall(packet(0x82, struct.pack("!H", 1) + enc_str(topic) + b"\x00"))
            req = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}).encode()
            s.sendall(packet(0x30, enc_str("device/%s/request" % cfg["serial"]) + req))
            print("✓ Connecté à l'imprimante %s. Laisse cette fenêtre ouverte pendant que tu utilises le site." % cfg["ip"])
            s.settimeout(30); last_ping = time.time()
            while True:
                if time.time() - last_ping > 25: s.sendall(b"\xc0\x00"); last_ping = time.time()
                try: h, b = read_packet(s)
                except socket.timeout: continue
                if h >> 4 == 3:
                    tl = struct.unpack("!H", b[:2])[0]; off = 2 + tl + (2 if (h >> 1) & 3 else 0)
                    try: merge(json.loads(b[off:].decode("utf-8", "replace")))
                    except ValueError: pass
        except Exception as e:
            with lock: state["connected"] = False; state["error"] = str(e)
            print("✗ Imprimante injoignable :", e, "· nouvel essai dans 10 s")
            time.sleep(10)

def payload():
    with lock:
        units = []
        for a in sorted(state["ams"] or [], key=lambda a: int(a.get("id", 0))):
            try: aid = int(a.get("id", 0))
            except ValueError: continue
            trays = {int(t.get("id", 0)): t for t in a.get("tray", []) if str(t.get("id", "")).isdigit()}
            if aid >= 128 or len(units) >= 4: continue        # AMS HT : non géré pour l'instant
            slots = []
            for i in range(4):
                t = trays.get(i) or {}
                col = (t.get("tray_color") or "")[:6]
                if not t.get("tray_type") or len(col) != 6:
                    slots.append(None); continue
                slots.append({"c": "#" + col.upper(), "type": t.get("tray_type", ""), "brand": t.get("tray_sub_brands", ""), "idx": t.get("tray_info_idx", "")})
            units.append({"u": "ABCD"[len(units)], "slots": slots})
        return {"ok": True, "connected": state["connected"], "error": state["error"], "units": units, "updated": state["updated"]}

class H(BaseHTTPRequestHandler):
    def cors(self):
        o = self.headers.get("Origin", "")
        if o.startswith(ORIGINS): self.send_header("Access-Control-Allow-Origin", o)
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Vary", "Origin")
    def do_OPTIONS(self): self.send_response(204); self.cors(); self.end_headers()
    def do_GET(self):
        if self.path.split("?")[0] not in ("/ams", "/"): self.send_response(404); self.end_headers(); return
        data = json.dumps(payload()).encode()
        self.send_response(200); self.cors(); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

if __name__ == "__main__":
    cfg = load_config()
    threading.Thread(target=mqtt_loop, args=(cfg,), daemon=True).start()
    print("Pont AMS prêt : sur le site, Mon compte → Mon AMS → « Synchroniser depuis l'imprimante ».")
    print("(Pour changer d'imprimante : relance avec --reset. Pour arrêter : ferme cette fenêtre.)\n")
    try: ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except OSError: print("Le pont est déjà lancé dans une autre fenêtre."); time.sleep(5)
