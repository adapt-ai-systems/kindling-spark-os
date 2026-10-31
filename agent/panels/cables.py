"""Cables: every fabric cable in the fleet, both ends, from LLDP.

Each node reads its own ports. The two netdevs of a physical port (enp1s0f0np0
and enP2p1s0f0np0 are both p0) share one cable and one set of PHY counters, so
a port is read once. lldpd names what is at the far end: the switch and its
port, or another box and its netdev. Any node's page pairs the ends from every
node's /status.json, so a cable between two boxes is one row with both ends.

A cable is red when an end has no carrier, runs below 200G, or its uncorrected
error counters grew in the last 15 min. Health's FAB column raises the same
faults in the alarm strip, so this page adds no alarms of its own. The
switch's side of a cable (its port counters) is not read here.
"""
import collections
import json
import math
import os
import re
import time

from . import checks
from .common import HOSTNAME, boxes, esc, mk, read_file

PATH, TITLE = "/cables", "Cables"
REFRESH_S = 10
COUNTERS = checks.PHY_FAIL + (checks.PHY_FLAP,)
LOG_DIR = os.environ.get("AGENT_LOG_DIR", "/logs")
LLDP_STALE_S = 300                      # the snapshot runs every minute

_growth = checks.Growth()
_last_view: dict = {}


# --- this node ---------------------------------------------------------------

def _lldp(now: float) -> tuple[dict, str]:
    """netdev -> {(far chassis, far port)}, and why there is none. A port also
    hears its own sibling netdev, so this host's own name is dropped.

    lldpcli runs for root and the adm group only, so spark-log-snapshot.sh
    (root, every minute) saves its answer in the log directory."""
    path = os.path.join(LOG_DIR, "lldp.json")
    try:
        age = now - os.path.getmtime(path)
        with open(path) as f:
            out = f.read()
    except OSError as e:
        return {}, f"{path}: {type(e).__name__}: {e}"
    try:
        d = json.loads(out)
    except ValueError:
        return {}, f"{path} is not JSON: {out.strip()[:200]}"
    if age > LLDP_STALE_S:
        return {}, f"{path} is {age / 60:.0f} min old: spark-log-snapshot is not running"
    far = collections.defaultdict(set)
    for itf in (d.get("lldp") or [{}])[0].get("interface") or []:
        name = ((itf.get("chassis") or [{}])[0].get("name") or [{}])[0].get("value", "")
        if name == HOSTNAME:
            continue
        port = (itf.get("port") or [{}])[0]
        pid = (port.get("id") or [{}])[0]
        descr = (port.get("descr") or [{}])[0].get("value", "")
        # A switch names its port by ifname; a box sends its MAC as the id and its netdev as descr.
        far[itf.get("name")].add((name, pid.get("value", "") if pid.get("type") == "ifname" else descr))
    return far, ""


def sample(view: dict) -> dict:
    _last_view.update(view)
    now = time.time()
    far, err = _lldp(now)
    ports: dict = {}
    for link in checks._links():
        nd = link["netdev"]
        pp = read_file(f"/sys/class/net/{nd}/phys_port_name").strip()
        p = ports.setdefault(pp if not pp.startswith("(") else nd, {"netdevs": [], "far": set(), "links": []})
        p["netdevs"].append(nd)
        p["far"] |= far.get(nd, set())
        p["links"].append(link)
    out = []
    for name, p in sorted(ports.items()):
        # Both netdevs report the same port counters: read the first.
        totals = {k: v for k, v in checks._ethtool(p["netdevs"][0]).items() if k in COUNTERS}
        for k, v in totals.items():
            _growth.feed(now, (name, k), v)
        grew = collections.Counter()
        for _, (port, k), n, _ in _growth.recent(now):
            if port == name:
                grew[k] += n
        ls = p["links"]
        out.append({"port": name, "netdevs": p["netdevs"], "far": sorted(p["far"]),
                    "carrier": 1 if all(l["carrier"] == 1 for l in ls) else 0,
                    "speed": min((l["speed"] or 0) for l in ls),
                    "mtu": min((l["mtu"] or 0) for l in ls), "since_boot": totals, "grew": dict(grew)})
    return {"at": now, "lldp_error": err, "ports": out}


# --- the fleet ---------------------------------------------------------------

def _end(box: dict, port: str, reading: dict | None) -> dict:
    return {"box": box["name"], "port": port, "ours": True, "reading": reading}


def fleet(view: dict) -> tuple[list, list]:
    """(cables, notes). A cable is {a, b, state, text}; an end is {box, port,
    ours, reading}, where reading is that box's own sample of the port, or None."""
    bx = boxes(view)
    by_host = {b["host"]: b for b in bx if b["host"]}
    ports = {}                                  # (box name, port) -> reading
    netdev_port = {}                            # (box name, netdev) -> port
    notes = []
    for b in bx:
        c = (b["status"] or {}).get("cables")
        if not b["status"]:
            notes.append(f"{b['name']}: agent does not answer, its ends come from its peers")
        elif not c or "error" in c:
            notes.append(f"{b['name']}: no cable reading" + (f" ({c['error']})" if c else ""))
        else:
            if c.get("lldp_error"):
                notes.append(f"{b['name']}: no LLDP: {c['lldp_error']}")
            for p in c["ports"]:
                ports[(b["name"], p["port"])] = p
                for nd in p["netdevs"]:
                    netdev_port[(b["name"], nd)] = p["port"]
    cables: dict = {}
    for (name, port), p in ports.items():
        box = next(b for b in bx if b["name"] == name)
        mine = _end(box, port, p)
        if not p["far"]:
            cables[((name, port),)] = [mine, None]
            continue
        for chassis, far_port in p["far"]:
            peer = by_host.get(chassis)
            if peer:
                fp = netdev_port.get((peer["name"], far_port))
                if not fp:
                    m = re.search(r"np(\d+)$", far_port)
                    fp = f"p{m.group(1)}" if m else far_port
                key = tuple(sorted([(name, port), (peer["name"], fp)]))
                if key not in cables:
                    cables[key] = [mine, _end(peer, fp, ports.get((peer["name"], fp)))]
            else:
                cables[((name, port), (chassis, far_port))] = [
                    mine, {"box": chassis, "port": far_port, "ours": False, "reading": None}]
    out = []
    for a, b in cables.values():
        state, text = judge(a, b)
        out.append({"a": a, "b": b, "state": state, "text": text})
    order = {b["name"]: i for i, b in enumerate(bx)}
    out.sort(key=lambda c: (order.get(c["a"]["box"], 99), c["a"]["port"]))
    return out, notes


def judge(a: dict, b: dict | None) -> tuple[str, str]:
    bad, warn = [], []
    for e in (a, b):
        if not e or not e["ours"]:
            continue
        r, who = e["reading"], f"{e['box']} {e['port']}"
        if r is None:
            warn.append(f"{who}: no reading from {e['box']}")
            continue
        if r["carrier"] != 1:
            bad.append(f"{who} has no carrier")
        elif (r["speed"] or 0) < checks.FABRIC_MBPS:
            bad.append(f"{who} at {r['speed']} Mb/s, not {checks.FABRIC_MBPS}")
        if 0 < r["mtu"] < checks.MTU_FLOOR:
            warn.append(f"{who} MTU {r['mtu']}, below {checks.MTU_FLOOR}")
        for k, n in sorted(r["grew"].items()):
            if k in checks.PHY_FAIL:
                bad.append(f"{who} {k} +{n}")
            elif r["carrier"] == 1:
                warn.append(f"{who} dropped and came back {n} times")
        if not r["far"]:
            warn.append(f"{who}: LLDP sees nothing at the far end")
    win = f" in the last {checks.WINDOW_S // 60} min" if any("+" in t for t in bad + warn) else ""
    if bad:
        return "fail", "; ".join(bad + warn) + win
    if warn:
        return "warn", "; ".join(warn) + win
    return "ok", "up at 200G, no errors growing"


def t_cables(**_):
    if not _last_view:
        return json.dumps({"error": "no sample yet"})
    cables, notes = fleet(_last_view)
    return json.dumps({"cables": [{"a": f"{c['a']['box']} {c['a']['port']}",
                                   "b": f"{c['b']['box']} {c['b']['port']}" if c["b"] else None,
                                   "state": c["state"], "text": c["text"]} for c in cables],
                       "notes": notes}, indent=2)


TOOLS = {
    "cables": (t_cables, "Every fabric cable in the fleet from LLDP: both ends (box port, or switch "
               "port), state (ok, warn, fail) and what was seen.", {}),
}


# --- page --------------------------------------------------------------------

def _ring(names: list, pairs: list) -> list:
    """Box order round the map: boxes cabled to each other sit side by side."""
    adj = {n: [] for n in names}
    for a, b in pairs:
        if a in adj and b in adj:
            adj[a].append(b)
            adj[b].append(a)
    order, seen = [], set()
    for start in sorted(names, key=lambda n: (len(adj[n]) != 1, names.index(n))):
        n = start
        while n and n not in seen:
            seen.add(n)
            order.append(n)
            n = next((m for m in adj[n] if m not in seen), None)
    return order


def _map(cables: list, names: list) -> str:
    pairs = [(c["a"]["box"], c["b"]["box"]) for c in cables if c["b"] and c["b"]["ours"]]
    order = _ring(names, pairs)
    cx, cy, rx, ry = 320, 190, 250, 140
    pos = {n: (cx + rx * math.cos(-math.pi / 2 + i * 2 * math.pi / len(order)),
               cy + ry * math.sin(-math.pi / 2 + i * 2 * math.pi / len(order))) for i, n in enumerate(order)}
    switches = sorted({c["b"]["box"] for c in cables if c["b"] and not c["b"]["ours"]})
    for i, s in enumerate(switches):
        pos[s] = (cx + (i - (len(switches) - 1) / 2) * 110, cy)
    lines = ""
    for c in cables:
        a = pos.get(c["a"]["box"])
        b = pos.get(c["b"]["box"]) if c["b"] else None
        if not a or not b:
            continue
        tip = f"{c['a']['box']} {c['a']['port']} - {c['b']['box']} {c['b']['port']}: {c['text']}"
        lines += (f"<line class={c['state']} x1={a[0]:.0f} y1={a[1]:.0f} x2={b[0]:.0f} y2={b[1]:.0f}>"
                  f"<title>{esc(tip)}</title></line>")
    nodes = ""
    for n, (x, y) in pos.items():
        w = 64 if n in names else 96
        nodes += (f"<rect class=\"{'sw' if n not in names else ''}\" x={x - w / 2:.0f} y={y - 13:.0f} "
                  f"width={w} height=26 rx=4></rect><text x={x:.0f} y={y + 4:.0f}>{esc(n)}</text>")
    return f"<svg class=cm viewBox=\"0 0 640 380\">{lines}{nodes}</svg>"


def _speed(r: dict) -> str:
    return f"{r['speed'] // 1000}G"


def _errors(r: dict) -> int:
    return sum(v for k, v in r["since_boot"].items() if k in checks.PHY_FAIL)


def page(view: dict, msg: str = "") -> str:
    cables, notes = fleet(view)
    names = [b["name"] for b in boxes(view)]
    count = collections.Counter(c["state"] for c in cables)
    rows = ""
    for c in cables:
        a, b = c["a"], c["b"]
        ends = [e for e in (a, b) if e and e["ours"]]
        rs = [e["reading"] for e in ends]

        def both(f):
            vals = ["?" if r is None else str(f(r)) for r in rs]
            return " / ".join(vals) if len(set(vals)) > 1 else vals[0]

        far = (f"{esc(b['box'])} {esc(b['port'])}" if b else "<span class=dim>nothing seen</span>")
        state = {"fail": "bad", "warn": "warn"}.get(c["state"], "")
        text = esc(c["text"]) if c["state"] != "ok" else f"<span class=dim>{esc(c['text'])}</span>"
        rows += (f"<tr><td class=n>{mk(state)}{esc(a['box'])} {esc(a['port'])}</td><td>{far}</td>"
                 f"<td>{both(_speed)}</td><td>{both(lambda r: r['carrier'])}</td><td>{both(lambda r: r['mtu'])}</td>"
                 f"<td class=r>{both(_errors)}</td>"
                 f"<td class=r>{both(lambda r: r['since_boot'].get(checks.PHY_FLAP, 0))}</td>"
                 f"<td class=t>{text}</td></tr>")
    note = "".join(f"<div class=lead>{esc(n)}</div>" for n in notes)
    warn = f"<span class=c-warn>{count['warn']} warn</span>" if count["warn"] else "0 warn"
    fail = f"<span class=c-bad>{count['fail']} fail</span>" if count["fail"] else "0 fail"
    return f"""<div class=vh><h3>Cables</h3><span>{len(cables)} cables from LLDP &middot;
{count['ok']} ok &middot; {warn} &middot; {fail}</span></div>{note}
<div class="sec scroll">{_map(cables, names)}</div>
<div class=scroll><table class=tb><tr><th>End A</th><th>End B</th><th>Speed</th><th>Carrier</th><th>MTU</th>
<th class=r>Errors since boot</th><th class=r>Link drops</th><th>Detail</th></tr>
{rows or '<tr><td colspan=8 class=dim>no cables seen</td></tr>'}</table></div>
<div class=foot><span><b>Errors</b> uncorrected PHY counters ({', '.join(checks.PHY_FAIL)}), both ends</span>
<span><b>Red</b> no carrier, below {checks.FABRIC_MBPS // 1000}G, or errors grew in the last {checks.WINDOW_S // 60} min</span>
<span><b>a / b</b> the two ends differ</span></div>"""
