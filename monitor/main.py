"""Monitor sAERO: one run = read every Aero chain + markets, update state, publish snapshot, notify."""
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import time as wall_clock
from zoneinfo import ZoneInfo

from . import aero as A
from . import alerts as N
from . import chain as C
from . import market as M
from . import marketdata as MD
from . import report as R
from . import tracker as T
from .rpc import Rpc

ROOT = Path(__file__).resolve().parent.parent


LOG_LINES = []
RPC = {}


def log(msg):
    line = f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def rpc_summaries():
    return {C.chain_short(cid): r.summary() for cid, r in RPC.items()}


def write_run_log(out):
    try:
        lines = LOG_LINES[-600:]
        for name, s in rpc_summaries().items():
            lines.append(f"rpc {name}: " + json.dumps(s))
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


def publish_site(out):
    """Copies the page, the app manifest, the service worker and the icons next to the data."""
    site = ROOT / "site"
    if site.is_dir():
        for f in site.iterdir():
            if f.is_file():
                shutil.copy(f, out / f.name)
    (out / ".nojekyll").write_text("")


def routing_targets(state, cfg, chains_cfg):
    """Largest pools on Base plus the largest ones of every other chain the aggregators cover."""
    rc = cfg["routing"]
    pinned = [k if ":" in k else f"8453:{k.lower()}" for k in rc.get("pinned_gauges", [])]
    by_ap = [k for k, _ in sorted(state["ap"].items(), key=lambda kv: -kv[1])]
    keys = pinned + [k for k in by_ap if A.chain_of(k) == C.BASE_CHAIN][: rc["top_pools"]]
    for cid_s, ccfg in chains_cfg.items():
        if int(cid_s) != C.BASE_CHAIN and (ccfg.get("kyber") or ccfg.get("velora")):
            keys += [k for k in by_ap if A.chain_of(k) == int(cid_s)][: rc.get("top_pools_other_chains", 3)]
    out = []
    for k in dict.fromkeys(keys):
        m = state["gauges"].get(k) or {}
        if m.get("pool") and m.get("t0") and m.get("t1"):
            out.append((k, A.chain_of(k), m["pool"], m["t0"], m["t1"]))
    return out


def tracker_settings(state, cfg, log):
    """Size of the simulation. It starts once and then keeps collecting: nothing in a run resets it."""
    tr = state.get("tracker")
    size = (tr or {}).get("size") or float(cfg.get("tracker", {}).get("size", 235_000))
    return size, not tr


def run(args):
    cfg = json.loads((ROOT / "config.json").read_text())
    # personal settings come from repository secrets, never from public files
    private_ids = [int(x) for x in re.findall(r"\d+", os.environ.get("MY_POSITIONS", ""))]
    cfg["my_positions"] = sorted(set(int(x) for x in cfg.get("my_positions", [])) | set(private_ids))
    chains_cfg = cfg["chains"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    t_start = wall_clock()
    read_deadline = t_start + float(cfg.get("read_budget_s", 360))
    min_interval = int(os.environ.get("MIN_INTERVAL_S") or 0)
    if min_interval:
        # automatic runs (schedule and cycle) can overlap: skip when another run collected moments ago
        prev = load_json(out / "latest.json", {}) or {}
        gen = int(prev.get("generated") or 0)
        failed_since = int((prev.get("status") or {}).get("last_error_at") or 0) > gen
        if now - gen < min_interval and not failed_since:
            log(f"skipped: the last collection was {(now - gen) // 60} min ago")
            (out / ".skipped").write_text(str(now))
            return
    state = load_json(out / "state.json.gz")
    first_run = not state
    state = A.new_state(now) if first_run else A.migrate(state, now, log)
    status = {"started": now, "errors": [], "first_run": first_run,
              "trigger": os.environ.get("GITHUB_EVENT_NAME") or "local"}

    def rpc_for(cid):
        if cid not in RPC:
            ccfg = chains_cfg[str(cid)]
            RPC[cid] = Rpc(ccfg["rpc"], name=C.chain_short(cid), log=log, pace=float(ccfg.get("pace_s", 0)))
        return RPC[cid]

    base_rpc = rpc_for(C.BASE_CHAIN)
    params = C.read_params(base_rpc)
    listed = [c for c in params["chains"] if c != C.BASE_CHAIN] or [int(c) for c in chains_cfg if int(c) != C.BASE_CHAIN]
    chain_ids = [C.BASE_CHAIN] + [c for c in listed if str(c) in chains_cfg and not chains_cfg[str(c)].get("off")]
    skipped = [c for c in listed if c not in chain_ids]
    log(f"cooldown {params['cooldown'] / 3600:.2f} h, max reduction {params['max_reduction']} s, "
        f"AERO {params['aero']}, chains {params['chains']}" + (f", not configured {skipped}" if skipped else ""))
    for cid in chain_ids:
        A.chain_state(state, cid)
        rpc_for(cid)

    # 1. events of every chain, read in parallel (each worker touches only its own chain state)
    def read_chain(cid):
        cs = state["chains"][str(cid)]
        rpc = RPC[cid]
        ccfg = chains_cfg[str(cid)]
        try:
            head, head_ts = A.chain_head(rpc, cid, cs, int(ccfg.get("lag", 2)), now, log)
            if args.max_blocks and not cs.get("last_block"):
                cs["last_block"] = head - args.max_blocks
            lp = C.read_leaf_params(rpc)
            cs["params"] = {k: v for k, v in lp.items() if v is not None} or cs.get("params", {})
            events = A.fetch_voter_events(rpc, cid, cs, head, ccfg, log, deadline=read_deadline)
        except Exception as e:
            log(f"{C.chain_short(cid)}: read failed: {type(e).__name__}: {str(e)[:300]}")
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        try:
            # the voter events above are already counted as read: a fee failure must not drop them
            events += A.fetch_reward_events(rpc, cid, cs, head, log, deadline=read_deadline, now=now)
        except Exception as e:
            log(f"{C.chain_short(cid)}: fee read failed: {type(e).__name__}: {str(e)[:300]}")
        return {"ok": True, "events": events, "head": head}

    with ThreadPoolExecutor(max_workers=len(chain_ids)) as ex:
        results = dict(zip(chain_ids, ex.map(read_chain, chain_ids)))

    # 2. apply and refresh chain by chain
    new_programs = []
    first_read = set()        # chains read successfully for the first time: their backlog is not news
    for cid in chain_ids:
        cs = state["chains"][str(cid)]
        res = results[cid]
        cs["status"] = {"ok": res["ok"], "error": res.get("error"), "at": now if res["ok"] else
                        (cs.get("status") or {}).get("at")}
        if not res["ok"]:
            status["errors"].append(f"{C.chain_short(cid)}: {res['error']}")
            continue
        if not cs.get("first_ok"):
            cs["first_ok"] = now
            first_read.add(cid)
        rpc = RPC[cid]
        try:
            cd = (cs.get("params") or {}).get("cooldown") or params["cooldown"]
            A.apply_events(state, cid, res["events"], cd)
            A.refresh_metadata(rpc, cid, state, log)
            A.refresh_tokens(rpc, cid, state, cfg, log)
            A.refresh_metadata(rpc, cid, state, log)   # gauges found while rebuilding positions
            programs = A.refresh_incentives(rpc, cid, state, now, log)
            if cid not in first_read:
                new_programs += programs
            A.refresh_metadata(rpc, cid, state, log)   # symbols of reward tokens
            A.refresh_allocation_power(rpc, cid, state, now, log)
            A.refresh_eligibility(rpc, cid, state, now, log)
        except Exception as e:
            cs["status"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "at": now}
            status["errors"].append(f"{C.chain_short(cid)}: {e}")
            log(f"{C.chain_short(cid)}: refresh failed: {traceback.format_exc()[-600:]}")
    A.record_ap_history(state, now)
    if not state["chains"][str(C.BASE_CHAIN)]["status"].get("ok"):
        raise RuntimeError("Base could not be read: " + str(results[C.BASE_CHAIN].get("error")))

    # 3. prices, rewards, simulation, routing
    want = {A.tkey(C.BASE_CHAIN, params["aero"])}
    want |= {A.tkey(p[1], p[3]) for p in state["fees_pending"]}
    want |= {p["tok"] for p in state["incentives"].values()}
    want |= {A.tkey(p.get("chain", C.BASE_CHAIN), p["tok"]) for p in state["incentives_pending"]}
    targets = routing_targets(state, cfg, chains_cfg)
    for k, cid, pool, t0, t1 in targets:
        want |= {A.tkey(cid, t0), A.tkey(cid, t1)}
    prices = M.fetch_prices(want, state["prices"], now, log, chains_cfg, state["erc20"],
                            state.setdefault("dex_slugs", {}))
    new_inc, fresh_fees = A.settle_rewards(state, prices, now, log)
    new_inc = list(dict.fromkeys(new_inc + new_programs))

    # hourly market snapshot (pool volumes, pair volume on every DEX, Aero share per chain), kept as history
    if not args.no_market:
        try:
            MD.discover(state, now, log)
            MD.refresh(state, chains_cfg, now, log)
        except Exception as e:
            status["errors"].append(f"market: {e}")
            log(f"market data failed: {traceback.format_exc()[-400:]}")

    # paper allocation of a fixed size, followed through every cooldown
    T.RULES.clear()
    T.RULES.update(cfg.get("strategies") or {})
    # options loaded from the page ("Carica queste opzioni nelle simulazioni"): kept until the next load
    raw_opts = (os.environ.get("OPTIONS_JSON") or "").strip()
    if raw_opts:
        try:
            state["options"] = {**T.parse_options(json.loads(raw_opts)), "at": now}
            log("options: loaded from the page: " + T.describe_options(state["options"]))
        except Exception as e:
            status["errors"].append(f"options: {e}")
            log(f"options: not loaded ({str(e)[:160]})")
    T.OPTIONS.clear()
    T.OPTIONS.update(state.get("options") or {})
    if T.OPTIONS:
        log("options: " + T.describe_options(T.options()))
    size, restart = tracker_settings(state, cfg, log)
    if restart:
        state["tracker"] = T.new_tracker(size, now)
        log(f"tracker: new collection with {size:,.0f} sAERO")
    tr = state["tracker"]
    aero_key = A.tkey(C.BASE_CHAIN, params["aero"])
    T.accrue(tr, state, prices, fresh_fees, now, params["cooldown"])
    latest_reco = T.snapshot(tr, state, prices, now, params["cooldown"], params["max_gauges"])
    tracker_report = T.summary(tr, state, now, prices.get(aero_key), params["cooldown"], latest_reco)
    log(f"tracker: size {size:,.0f}, " + ", ".join(
        f"{x['id']} {x['usd_total']:.2f} USD in {x['hours']:.1f} h, now {x['now']['pred_47h']:.2f} USD/47h on "
        f"{len(x['now']['pools'])} pools (" + " ".join(f"{c} {pc}%" for c, _, pc in x["now"]["chains"]) + ")"
        for x in tracker_report["strategies"]))
    log("tracker: realized per day by the allocations saved every run (" + tracker_report["rank_by"] + "): " + ", ".join(
        f"{x['id']} {x['perf']['rate_' + tracker_report['rank_by']]}" for x in
        sorted(tracker_report["strategies"], key=lambda x: tracker_report["ranking"].index(x["id"]))))

    routing, routing_errors = {}, {}
    if not args.no_routing and now - state.get("last_routing", 0) >= 55 * 60:
        state["last_routing"] = now
        aero_pools = {}
        for k, m in state["gauges"].items():
            if m.get("pool"):
                aero_pools.setdefault(A.chain_of(k), set()).add(m["pool"])
        try:
            routing, routing_errors = M.routing_share(targets, aero_pools, prices, state["erc20"],
                                                      cfg["routing"]["usd_sizes"], chains_cfg, log)
            R.record_routing(state, routing, now)
        except Exception as e:
            status["errors"].append(f"routing: {e}")
            log(f"routing failed: {e}")

    status["rpc"] = rpc_summaries()
    status["finished"] = int(time.time())
    status["seconds"] = round(wall_clock() - t_start, 1)
    report = R.build(state, params, prices, now, cfg, routing_errors, status)
    report["block"] = state["chains"][str(C.BASE_CHAIN)].get("head")
    report["page_url"] = N.page_url(cfg)
    report["repo"] = os.environ.get("GITHUB_REPOSITORY") or cfg.get("repo")

    # notifications
    tz = ZoneInfo(cfg.get("timezone", "Europe/Rome"))
    local = dt.datetime.fromtimestamp(now, tz)
    fresh = N.evaluate(report, state, cfg, now, new_inc)
    quiet = first_run or state.get("migrated_at") == now
    if quiet:
        # the backlog of a first read is not news: remember it silently
        for a in fresh:
            if not a.get("private"):
                state["alerts"][a["key"]] = now
        fresh = [a for a in fresh if a.get("private")]
        if first_run:
            state["last_digest"] = local.strftime("%Y-%m-%d")
    if N.topic_of(cfg) and not state.get("welcome_sent"):
        fresh.insert(0, {"key": f"welcome:{now}", "prio": 3, "tags": ["satellite"],
                         "title": "Monitor sAERO attivo",
                         "body": f"Controllo Aero ogni 15 minuti su {len(chain_ids)} chain. Ora seguo "
                                 f"{report['totals']['positions']} posizioni e {len(report['pools'])} pool. "
                                 f"Ti scrivo qui quando succede qualcosa di rilevante, più un riepilogo ogni mattina."})
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
    pos_index, pos_shards = R.positions_export(state, params, now)
    pos_dir = out / "pos"
    pos_dir.mkdir(exist_ok=True)
    for old in pos_dir.glob("*.json"):
        if int(old.stem) not in pos_shards:
            old.unlink()
    for k, shard in pos_shards.items():
        save_json(pos_dir / f"{k}.json", shard)
    save_json(out / "positions.json", pos_index)
    save_json(out / "tracker.json", tracker_report)
    save_json(out / "state.json.gz", state, gz=True)
    publish_site(out)
    write_run_log(out)
    log(f"done in {wall_clock() - t_start:.0f}s: {len(report['pools'])} pools on {len(chain_ids)} chains, "
        f"{len(report['calendar'])} calendar rows, {len(report['incentives_upcoming'])} upcoming incentives, "
        f"latest.json {(out / 'latest.json').stat().st_size / 1024:.0f} KB, "
        f"state {(out / 'state.json.gz').stat().st_size / 1024:.0f} KB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--max-blocks", type=int, default=0, help="limit the first backfill (testing)")
    ap.add_argument("--no-routing", action="store_true")
    ap.add_argument("--no-market", action="store_true")
    args = ap.parse_args()
    try:
        run(args)
    except Exception:
        traceback.print_exc()
        log("run failed: " + traceback.format_exc()[-800:])
        # record the failure for the page, keep the previous data untouched
        out = Path(args.out)
        latest = load_json(out / "latest.json", {}) or {}
        fails = latest.get("status", {}).get("consecutive_failures", 0) + 1
        latest.setdefault("status", {})
        latest["status"].update({"last_error": traceback.format_exc()[-1500:], "last_error_at": int(time.time()),
                                 "consecutive_failures": fails})
        out.mkdir(parents=True, exist_ok=True)
        if RPC:
            latest["status"]["rpc"] = rpc_summaries()
        save_json(out / "latest.json", latest)
        write_run_log(out)
        publish_site(out)
        if fails in (4, 24):
            cfg = json.loads((ROOT / "config.json").read_text())
            state = load_json(out / "state.json.gz", {"alerts": {}}) or {"alerts": {}}
            N.send([{"key": f"fail:{int(time.time())}", "prio": 4, "tags": ["warning"],
                     "title": "Monitor sAERO non riesce a leggere i dati",
                     "body": f"Le ultime {fails} esecuzioni sono fallite. Controllo GitHub Actions per i dettagli."}],
                   cfg, log, int(time.time()), state)
        sys.exit(0)


if __name__ == "__main__":
    main()
