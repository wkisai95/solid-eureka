"""Radar sAERO monitor: one run = read chain + markets, update state, publish snapshot, notify."""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

from . import aero as A
from . import alerts as N
from . import chain as C
from . import market as M
from . import report as R
from .rpc import Rpc

ROOT = Path(__file__).resolve().parent.parent


LOG_LINES = []
RPC = {}


def log(msg):
    line = f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def write_run_log(out):
    try:
        lines = LOG_LINES[-400:]
        if RPC.get("rpc"):
            lines.append("rpc: " + json.dumps(RPC["rpc"].summary()))
        Path(out, "run.log").write_text("\n".join(lines) + "\n")
    except Exception as e:
        print("could not write run.log:", e)


def load_json(path, default=None):
    try:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rt") as f:
                return json.load(f)
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"could not read {path}: {e}")
        return default


def save_json(path, obj, gz=False):
    tmp = Path(str(path) + ".tmp")
    if gz:
        with gzip.open(tmp, "wt") as f:
            json.dump(obj, f, separators=(",", ":"))
    else:
        tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(path)


def routing_targets(state, cfg):
    pinned = [k if ":" in k else f"8453:{k.lower()}" for k in cfg["routing"].get("pinned_gauges", [])]
    by_ap = [k for k, _ in sorted(state["ap"].items(), key=lambda kv: -kv[1])]
    keys = list(dict.fromkeys(pinned + by_ap))[: cfg["routing"]["top_pools"] + len(pinned)]
    out = []
    for k in keys:
        m = state["gauges"].get(k) or {}
        if m.get("pool") and m.get("t0") and m.get("t1"):
            out.append((k, m["pool"], m["t0"], m["t1"]))
    return out


def run(args):
    cfg = json.loads((ROOT / "config.json").read_text())
    # personal settings come from repository secrets, never from public files
    private_ids = [int(x) for x in re.findall(r"\d+", os.environ.get("MY_POSITIONS", ""))]
    cfg["my_positions"] = sorted(set(int(x) for x in cfg.get("my_positions", [])) | set(private_ids))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    state = load_json(out / "state.json.gz")
    first_run = not state
    if first_run:
        state = A.new_state(now)
    status = {"started": now, "errors": [], "first_run": first_run}
    rpc = Rpc(cfg["rpc"]["base"], log=log)
    RPC["rpc"] = rpc

    head = rpc.block_number() - 2
    params = C.read_params(rpc)
    log(f"head {head}, cooldown {params['cooldown'] / 3600:.2f} h, max reduction {params['max_reduction']} s, "
        f"AERO {params['aero']}, chains {params['chains']}")
    if args.max_blocks and not state["last_block"]:
        state["last_block"] = head - args.max_blocks

    events = A.fetch_voter_events(rpc, state, head, log)
    A.apply_events(state, events, params["cooldown"])
    state["last_block"] = head
    A.refresh_metadata(rpc, state, log)
    A.refresh_tokens(rpc, state, cfg, log)
    A.refresh_metadata(rpc, state, log)   # gauges found while rebuilding positions
    A.apply_events(state, A.fetch_reward_events(rpc, state, head, log), params["cooldown"])
    A.refresh_metadata(rpc, state, log)   # symbols of reward tokens
    A.refresh_allocation_power(rpc, state, now, log)

    want = {params["aero"]} | {p[2] for p in state["fees_pending"]} | {p["tok"] for p in state["incentives"].values()}
    want |= {p["tok"] for p in state["incentives_pending"]}
    for k, pool, t0, t1 in routing_targets(state, cfg):
        want |= {t0, t1}
    prices = M.fetch_prices(want, state["prices"], now, log)
    new_inc = A.settle_rewards(state, prices, now, log)

    routing, routing_errors = {}, {}
    if not args.no_routing and now - state.get("last_routing", 0) >= 55 * 60:
        state["last_routing"] = now
        aero_pools = {m["pool"] for m in state["gauges"].values() if m.get("pool")}
        try:
            routing, routing_errors = M.routing_share(routing_targets(state, cfg), aero_pools, prices,
                                                      state["erc20"], cfg["routing"]["usd_sizes"], log)
            R.record_routing(state, routing, now)
        except Exception as e:
            status["errors"].append(f"routing: {e}")
            log(f"routing failed: {e}")

    status["rpc"] = rpc.summary()
    status["finished"] = int(time.time())
    report = R.build(state, params, prices, now, cfg, routing_errors, status)
    report["block"] = head
    report["page_url"] = N.page_url(cfg)

    # notifications
    tz = ZoneInfo(cfg.get("timezone", "Europe/Rome"))
    local = dt.datetime.fromtimestamp(now, tz)
    fresh = N.evaluate(report, state, cfg, now, new_inc)
    if first_run:
        # the backlog of the first read is not news: remember it without notifying
        for a in fresh:
            state["alerts"][a["key"]] = now
        fresh = []
        state["last_digest"] = local.strftime("%Y-%m-%d")
    if N.topic_of(cfg) and not state.get("welcome_sent"):
        fresh.insert(0, {"key": f"welcome:{now}", "prio": 3, "tags": ["satellite"],
                         "title": "Radar sAERO: monitor attivo",
                         "body": f"Controllo Aero ogni 15 minuti. Ora seguo {report['totals']['positions']} posizioni "
                                 f"su Base e {len(report['pools'])} pool. Ti scrivo qui quando succede qualcosa di "
                                 f"rilevante, più un riepilogo ogni mattina."})
        state["welcome_sent"] = True
    if os.environ.get("TEST_NOTIFY") == "true":
        fresh.insert(0, {"key": f"test:{now}", "prio": 3, "tags": ["white_check_mark"],
                         "title": "Notifica di prova", "body": "Se leggi questo messaggio, le notifiche funzionano."})
    if local.hour >= cfg.get("digest_hour", 8) and state.get("last_digest") != local.strftime("%Y-%m-%d"):
        fresh.append(N.digest(report, cfg, now))
        state["last_digest"] = local.strftime("%Y-%m-%d")
    if fresh:
        N.send(fresh, cfg, log, now, state)
    report["alerts"] = state.get("alert_log", [])[-40:][::-1]

    # keepalive marker for the workflow (scheduled runs stop after 60 days without commits)
    if now - state.get("keepalive", now) > 25 * 86400:
        (out / ".keepalive_needed").write_text(str(now))
        state["keepalive"] = now

    public_report = {k: v for k, v in report.items() if k != "mine"}
    public_report["personal_alerts"] = bool(cfg["my_positions"]) and bool(N.topic_of(cfg))
    save_json(out / "latest.json", public_report)
    save_json(out / "positions.json", R.positions_export(state, params, now))
    save_json(out / "state.json.gz", state, gz=True)
    site = ROOT / "site" / "index.html"
    if site.exists():
        shutil.copy(site, out / "index.html")
    (out / ".nojekyll").write_text("")
    write_run_log(out)
    log(f"done: {len(report['pools'])} pools, {len(report['calendar'])} calendar rows, "
        f"{len(report['incentives_upcoming'])} upcoming incentives, latest.json "
        f"{(out / 'latest.json').stat().st_size / 1024:.0f} KB, state {(out / 'state.json.gz').stat().st_size / 1024:.0f} KB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--max-blocks", type=int, default=0, help="limit the first backfill (testing)")
    ap.add_argument("--no-routing", action="store_true")
    args = ap.parse_args()
    try:
        run(args)
    except Exception:
        traceback.print_exc()
        # record the failure for the page, keep the previous data untouched
        out = Path(args.out)
        latest = load_json(out / "latest.json", {}) or {}
        fails = latest.get("status", {}).get("consecutive_failures", 0) + 1
        latest.setdefault("status", {})
        latest["status"].update({"last_error": traceback.format_exc()[-1500:], "last_error_at": int(time.time()),
                                 "consecutive_failures": fails})
        out.mkdir(parents=True, exist_ok=True)
        if RPC.get("rpc"):
            latest["status"]["rpc"] = RPC["rpc"].summary()
        save_json(out / "latest.json", latest)
        write_run_log(out)
        site = ROOT / "site" / "index.html"
        if site.exists():
            shutil.copy(site, out / "index.html")
        (out / ".nojekyll").write_text("")
        if fails in (4, 24):
            cfg = json.loads((ROOT / "config.json").read_text())
            state = load_json(out / "state.json.gz", {"alerts": {}}) or {"alerts": {}}
            N.send([{"key": f"fail:{int(time.time())}", "prio": 4, "tags": ["warning"],
                     "title": "Radar sAERO non riesce a leggere i dati",
                     "body": f"Le ultime {fails} esecuzioni sono fallite. Controllo GitHub Actions per i dettagli."}],
                   cfg, log, int(time.time()), state)
        sys.exit(0)


if __name__ == "__main__":
    main()
