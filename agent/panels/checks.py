"""Health checks: one function per column of the Health page.

read() takes this node's facts once per sample, so two checks that need the
same command do not run it twice. A check judges those facts and returns
(state, text). The state is "ok", "warn", "fail", or "na" when the check does
not apply on this box. The text says what was seen, in plain words.

To add a check, write a function that takes the facts and list it in CHECKS
with its column label and a one-line description.

Error counters are judged on growth, never on their totals, which only reset
at a reboot. Growth counts for WINDOW_S, so a burst of errors stays on the page
long enough to be seen, and then clears by itself.
"""
import collections
import ipaddress
import json
import os
import socket
import statistics
import time

from . import power
from .common import read_file, run

WINDOW_S = 900
DISK_WARN, DISK_FAIL = 85, 95           # % used
MEM_WARN_GIB = 10                       # MemAvailable
TEMP_WARN, TEMP_FAIL = 80, 90           # GPU, degrees C
FABRIC_MBPS = 200000                    # every GB10 fabric port runs at 200G
MTU_FLOOR = 4200                        # RoCE needs a 4096 RDMA MTU; above that the workload picks
SYMBOL_RATE_WARN = 50000                # corrected symbols/s on a lane: pre-FEC BER ~1e-6
PEER_TRIES = 10                         # loss and the median round trip cover this many samples
PEER_SLOW_MS = 1.0
PEER_MIN_TRIES = 3                      # judge the round trip only from this many samples on

# ethtool -S counters on each fabric port.
PHY_FAIL = ("rx_crc_errors_phy", "rx_pcs_symbol_err_phy", "rx_symbol_err_phy", "tx_errors_phy")
PHY_WARN = ("rx_discards_phy", "tx_discards_phy")
PHY_FLAP = "link_down_events_phy"
PHY_LANE = "rx_err_lane_"               # pre-FEC corrected symbols, one counter per lane
# RDMA counters (sysfs). Congestion marks, packets out of order and flushed
# work requests come with serve traffic and engine restarts: shown, not judged.
RDMA_ERR = ("err", "retrans", "timeout", "nak", "discard", "drop", "rnr", "seq")
RDMA_INFO = ("ecn", "cnp", "out_of_sequence", "flush")


class Growth:
    """Counter growth over the last WINDOW_S: feed totals in, read growth out."""

    def __init__(self):
        self.last: dict = {}
        self.hist: list = []            # (time, key, growth, growth per second)

    def feed(self, now: float, key, value: int):
        prev = self.last.get(key)
        self.last[key] = (now, value)
        if prev and value > prev[1]:
            grew = value - prev[1]
            self.hist.append((now, key, grew, grew / max(now - prev[0], 1e-3)))

    def recent(self, now: float) -> list:
        self.hist = [h for h in self.hist if now - h[0] <= WINDOW_S]
        return self.hist


_phy_growth, _ib_growth = Growth(), Growth()
_peer_hist: dict = collections.defaultdict(lambda: collections.deque(maxlen=PEER_TRIES))


# --- facts -------------------------------------------------------------------

def _json(cmd: list[str]):
    try:
        return json.loads(run(cmd, 10) or "null")
    except ValueError:
        return None


def _int(path: str):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _links() -> list[dict]:
    """The fabric ports: every RDMA link with its netdev's speed, carrier,
    MTU and IPv4 addresses."""
    addrs = {a["ifname"]: [f"{i['local']}/{i['prefixlen']}" for i in a.get("addr_info") or []]
             for a in _json(["ip", "-j", "-4", "addr"]) or []}
    out = []
    for r in _json(["rdma", "-j", "link", "show"]) or []:
        nd = r.get("netdev") or ""
        sysfs = f"/sys/class/net/{nd}"
        out.append({"rdma": r.get("ifname"), "netdev": nd, "state": r.get("state"),
                    "speed": _int(f"{sysfs}/speed"), "carrier": _int(f"{sysfs}/carrier"),
                    "mtu": _int(f"{sysfs}/mtu"), "addrs": addrs.get(nd, [])})
    return out


def _ethtool(netdev: str) -> dict:
    out = {}
    for line in run(["ethtool", "-S", netdev], 10).splitlines():
        k, _, v = line.strip().partition(": ")
        if k.endswith("_phy") and v.strip().isdigit():
            out[k] = int(v)
    return out


def _ib_counters(dev: str) -> dict:
    out = {}
    for group in ("hw_counters", "counters"):
        d = f"/sys/class/infiniband/{dev}/ports/1/{group}"
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            v = _int(f"{d}/{n}")
            if v is not None:
                out[n] = v
    return out


def _reasons() -> list[str]:
    """Clock event reasons nvidia-smi calls Active, but for idling and the
    application clocks, which are the cap itself."""
    out = []
    for line in run(["nvidia-smi", "-q", "-d", "PERFORMANCE"], 10).splitlines():
        k, _, v = line.strip().rpartition(":")
        if v.strip() == "Active" and k.strip() not in ("Idle", "Applications Clocks Setting"):
            out.append(k.strip())
    return out


def _units(*names) -> dict:
    """unit -> its ActiveState, or None when this box has no such unit."""
    out, cur = {}, {}
    for line in run(["systemctl", "show", "-p", "Id,LoadState,ActiveState", *names]).splitlines() + [""]:
        if line:
            k, _, v = line.partition("=")
            cur[k] = v
        elif cur:
            out[cur.get("Id")] = None if cur.get("LoadState") == "not-found" else cur.get("ActiveState")
            cur = {}
    return out


def _cuda() -> str | None:
    for line in run(["ldconfig", "-p"]).splitlines():
        if "libcudart.so" in line and "=>" in line:
            path = line.split("=>", 1)[1].strip()
            if os.path.exists(path):
                return path
    return None


def _disk() -> tuple[str, float, float]:
    """(mount, % used, size in TB) of the disk that holds the data. On Spark
    OS / is RAM, and the DGX OS disk is mounted at /run/sparkos/host."""
    path = "/run/sparkos/host" if os.path.ismount("/run/sparkos/host") else "/"
    st = os.statvfs(path)
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    return path, 100.0 * used / max(used + avail, 1), st.f_blocks * st.f_frsize / 1e12


def _probe(src: str, dst: str, port: int) -> float | None:
    """Round trip of a TCP connect from one fabric address to a peer's, in ms.

    The agent may not open raw sockets, so it cannot ping. A TCP handshake
    crosses the same link, and a refusal is a reply too: only silence is loss.
    """
    t = time.monotonic()
    try:
        with socket.create_connection((dst, port), timeout=1, source_address=(src, 0)):
            pass
    except ConnectionRefusedError:
        pass
    except OSError:
        return None
    return (time.monotonic() - t) * 1000


def _peers(links: list, view: dict) -> list[dict]:
    """Probe every other box's fabric address that shares a subnet with ours."""
    theirs = []
    for n in view["nodes"].values():
        st = n.get("agent") or {}
        if st.get("node") in (None, view["host"]):
            continue
        name = n.get("alias") or st["node"]
        for f in (st.get("health") or {}).get("fabric") or []:
            theirs.append((name, ipaddress.ip_interface(f["addr"])))
    pairs = {}
    for link in links:
        for a in link["addrs"]:
            mine = ipaddress.ip_interface(a)
            for name, peer in theirs:
                if peer.network == mine.network and peer.ip != mine.ip:
                    pairs[(link["netdev"], str(mine.ip), str(peer.ip))] = name
    out = []
    # One after another: probes run side by side in Python time each other's
    # waits, not the link.
    for (nd, src, dst), name in pairs.items():
        h = _peer_hist[(nd, dst)]
        h.append(_probe(src, dst, view["port"]))
        got = [x for x in h if x is not None]
        out.append({"netdev": nd, "box": name, "ip": dst, "tries": len(h), "answered": len(got),
                    "ms": statistics.median(got) if got else None})
    return out


def read(view: dict) -> dict:
    now = time.time()
    links = _links()
    for link in links:
        for k, v in _ethtool(link["netdev"]).items():
            _phy_growth.feed(now, (link["netdev"], k), v)
        for k, v in _ib_counters(link["rdma"]).items():
            _ib_growth.feed(now, (link["rdma"], k), v)
    mem = {k: int(v.split()[0]) for k, _, v in
           (line.partition(":") for line in read_file("/proc/meminfo").splitlines()) if v.strip()}
    routes = sorted(_json(["ip", "-j", "-4", "route", "show", "default"]) or [],
                    key=lambda r: r.get("metric", 0))
    return {"now": now, "gpu": power.sample(), "reasons": _reasons(), "links": links,
            "phy": _phy_growth.recent(now), "ib": _ib_growth.recent(now), "peers": _peers(links, view),
            "disk": _disk(), "mem_gib": mem.get("MemAvailable", 0) / 2**20,
            "mem_total_gib": mem.get("MemTotal", 0) / 2**20,
            "route": routes[0].get("dev", "") if routes else None,
            "pagesize": os.sysconf("SC_PAGE_SIZE"), "cuda": _cuda(),
            "units": _units("sparkos-netsync.path", "sparkos-gpucap.path")}


# --- checks ------------------------------------------------------------------

def c_pages(f):
    kb = f["pagesize"] // 1024
    return ("ok", "64K kernel pages") if kb == 64 else ("warn", f"{kb}K pages, not the 64K kernel")


def c_cuda(f):
    if f["cuda"]:
        return "ok", f"CUDA runtime at {f['cuda']}"
    return "fail", "no CUDA runtime on the loader path"


def c_sync(f):
    st = f["units"].get("sparkos-netsync.path")
    if st is None:
        return "na", "no sparkos-netsync on this box"
    if st == "active":
        return "ok", "live network edits are copied to disk"
    return "warn", f"sparkos-netsync.path is {st}: live network edits will not survive a reboot"


def c_clock(f):
    g = f["gpu"]
    cap, mx = g.get("cap_mhz") or 0, g.get("max_mhz")
    sm, util = g.get("sm_mhz"), g.get("util_pct")
    if sm is None or not mx:
        return "fail", f"nvidia-smi gave no clocks: {g.get('error', '')}"
    if f["units"].get("sparkos-gpucap.path") not in (None, "active"):
        return "warn", f"sparkos-gpucap.path is {f['units']['sparkos-gpucap.path']}: a new cap will not apply"
    if cap and not g.get("applied"):
        return "fail", f"cap {cap} MHz is not applied: {g.get('apply_error') or 'no result yet'}"
    ceiling = cap or mx
    # An idle GPU drops its clock and lists reasons for it. Only a busy GPU
    # held below its ceiling is throttled.
    if util and sm < 0.95 * ceiling and f["reasons"]:
        return "fail", f"{sm:.0f} of {ceiling:.0f} MHz at {util:.0f}% util, held by {', '.join(f['reasons'])}"
    if util and util >= 50 and sm < 0.5 * ceiling:
        return "warn", f"{sm:.0f} of {ceiling:.0f} MHz at {util:.0f}% util"
    capt = f"cap {cap} MHz applied" if cap else "no cap"
    return "ok", f"{capt}, {sm:.0f} MHz at {util or 0:.0f}% util"


def c_fabric(f):
    if not f["links"]:
        return "na", "no fabric ports"
    bad, warn = [], []
    for link in f["links"]:
        nd = link["netdev"]
        if link["state"] != "ACTIVE" or link["carrier"] != 1:
            bad.append(f"{nd} down (state {link['state']}, carrier {link['carrier']})")
        elif link["speed"] != FABRIC_MBPS:
            bad.append(f"{nd} at {link['speed']} Mb/s, not {FABRIC_MBPS}")
        elif (link["mtu"] or 0) < MTU_FLOOR:
            warn.append(f"{nd} MTU {link['mtu']}, below {MTU_FLOOR}")
    up = {l["netdev"] for l in f["links"] if l["carrier"] == 1}
    grew = collections.Counter()
    fast = {}
    for _, (nd, k), n, rate in f["phy"]:
        grew[(nd, k)] += n
        if k.startswith(PHY_LANE):
            fast[(nd, k)] = max(rate, fast.get((nd, k), 0))
    for (nd, k), n in sorted(grew.items()):
        if k in PHY_FAIL:
            bad.append(f"{nd} {k} +{n}")
        elif k in PHY_WARN:
            warn.append(f"{nd} {k} +{n}")
        elif k == PHY_FLAP and nd in up:
            warn.append(f"{nd} dropped and came back {n} times")
    for (nd, k), rate in sorted(fast.items()):
        if rate > SYMBOL_RATE_WARN:
            warn.append(f"{nd} {k} {rate:,.0f} corrected symbols/s: marginal cable")
    win = f" in the last {WINDOW_S // 60} min" if f["phy"] else ""
    if bad:
        return "fail", "; ".join(bad + warn) + win
    if warn:
        return "warn", "; ".join(warn) + win
    return "ok", f"{len(f['links'])} ports up at {FABRIC_MBPS // 1000}G, MTU {MTU_FLOOR}+, no PHY errors"


def c_peers(f):
    if not f["links"]:
        return "na", "no fabric ports"
    bad, warn = [], []
    for p in f["peers"]:
        to = f"{p['box']} {p['ip']} from {p['netdev']}"
        if not p["answered"]:
            bad.append(f"no answer from {to}")
        elif p["answered"] < p["tries"]:
            bad.append(f"{100 - 100 * p['answered'] // p['tries']}% lost to {to}")
        elif p["tries"] >= PEER_MIN_TRIES and p["ms"] > PEER_SLOW_MS:
            warn.append(f"{p['ms']:.2f} ms to {to}")
    if bad:
        return "fail", "; ".join(bad + warn)
    if warn:
        return "warn", "; ".join(warn)
    if not f["peers"]:
        return "warn", "no other box seen on any fabric subnet"
    slow = max(p["ms"] for p in f["peers"])
    return "ok", f"{len(f['peers'])} fabric peers answer, slowest {slow:.2f} ms"


def c_rdma(f):
    if not f["links"]:
        return "na", "no fabric ports"
    grew = collections.Counter()
    for _, (dev, k), n, _ in f["ib"]:
        grew[(dev, k)] += n
    info = collections.Counter()
    errs = []
    for (dev, k), n in sorted(grew.items()):
        if any(w in k for w in RDMA_INFO):
            info[k] += n
        elif any(w in k for w in RDMA_ERR):
            errs.append(f"{dev} {k} +{n}")
    note = ("; seen, not judged: " + ", ".join(f"{k} +{n}" for k, n in info.items())) if info else ""
    if errs:
        return "warn", "; ".join(errs) + f" in the last {WINDOW_S // 60} min" + note
    return "ok", "no RDMA error growth" + note


def c_disk(f):
    path, pct, tb = f["disk"]
    text = f"{pct:.0f}% of {tb:.1f} TB used on {path}"
    return ("fail" if pct >= DISK_FAIL else "warn" if pct >= DISK_WARN else "ok"), text


def c_mem(f):
    text = f"{f['mem_gib']:.0f} of {f['mem_total_gib']:.0f} GiB available"
    return ("warn", text + ", little headroom") if f["mem_gib"] < MEM_WARN_GIB else ("ok", text)


def c_temp(f):
    t = f["gpu"].get("temp_c")
    if t is None:
        return "na", "nvidia-smi gave no temperature"
    state = "fail" if t >= TEMP_FAIL else "warn" if t >= TEMP_WARN else "ok"
    return state, f"GPU {t:.0f}°C"


def c_route(f):
    dev = f["route"]
    if dev is None:
        return "warn", "no default route"
    if dev.startswith("wl"):
        return "warn", f"default route over Wi-Fi ({dev}): the wired port is down"
    return "ok", f"default route via {dev}"


CHECKS = [
    ("64K", c_pages, "kernel page size"),
    ("CUDA", c_cuda, "CUDA runtime on the loader path"),
    ("SYNC", c_sync, "live network edits persist"),
    ("CLK", c_clock, "GPU clock cap applied, no throttle"),
    ("FAB", c_fabric, "fabric ports up, 200G, MTU, PHY errors"),
    ("PEER", c_peers, "fabric peers answer, no loss"),
    ("RDMA", c_rdma, "RDMA error counters"),
    ("DISK", c_disk, "disk use"),
    ("MEM", c_mem, "available memory"),
    ("TEMP", c_temp, "GPU temperature"),
    ("RTE", c_route, "default route not over Wi-Fi"),
]
