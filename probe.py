"""Temporary probe: the new market data code against the live APIs, on the live state."""
import gzip, io, json, sys, time, urllib.request
from monitor import marketdata as MD, aero as A, chain as C

logs = []
def log(m):
    logs.append(f"[{time.strftime('%H:%M:%S')}] {m}")
raw = urllib.request.urlopen("https://raw.githubusercontent.com/wkisai95/solid-eureka/data/state.json.gz", timeout=60).read()
state = json.load(gzip.GzipFile(fileobj=io.BytesIO(raw)))
cfg = json.load(open("config.json"))
chains_cfg = cfg.get("chains") or {}
now = int(time.time())
out = {}
t0 = time.time()
for i in range(3):
    MD.discover(state, now + i * 3700, log)
    time.sleep(3)
out["discover_s"] = round(time.time() - t0, 1)
state["market"]["at"] = 0
t0 = time.time()
MD.refresh(state, chains_cfg, now + 7400, log)
out["refresh_s"] = round(time.time() - t0, 1)
mk = state["market"]
out["chains"] = mk["chains"]
out["top"] = mk["top"]
out["gt"] = {c: {"at": g.get("at"), "n": len(g.get("pools") or []), "pools": (g.get("pools") or [])[:20]} for c, g in mk["gt"].items()}
uni, ok = MD.new_chain_universe(state, (cfg.get("strategies") or {}).get("nuove"))
out["universe_chains"] = sorted(ok)
rows = []
for k, v in mk["pools"].items():
    cid = A.chain_of(k)
    if cid in (8453,) and (v[4] or 0) < 20e6:
        continue
    rows.append([C.chain_short(cid), A.gauge_name(state, k), (state["gauges"][k] or {}).get("kind"), v, k in uni])
rows.sort(key=lambda r: (r[0], -(r[3][4] or 0)))
out["pools"] = rows
out["logs"] = logs
json.dump(out, sys.stdout, indent=1, default=str)
