#!/usr/bin/env python3
"""Host agent: general MCP tools for one node, served through mentat.

Runs beside the model containers rather than inside one, so the capabilities
that need a host (reading every process, ptrace, kernel messages) leave the
process that runs the model.

Tools return what the underlying command printed. A parsed view discards the
fields nobody thought to keep, and on 2026-08-25 the answer to where 100 GiB
of memory went was in `nvidia-smi --query-compute-apps`, which the curated GPU
tool did not carry. Correlating several sources is a separate tool, named for
the question it answers.

The agent registers its MCP endpoint with the local mentatd, and mentat-serve
merges it with every other node's, so clients reach every node's tools
through the router. Stdlib only, apart from the mentat shim's ray.register,
and it runs without that. Prototype on a spare port with:
    python3 spark-agent.py --port 8090
"""
import argparse
import concurrent.futures
import ipaddress
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import panels
from panels.common import HOSTNAME, read_file, run


class Server(ThreadingHTTPServer):
    """The HTTP listener.

    socketserver defaults `request_queue_size` to 5, and that is the listen()
    backlog. Past five connections the kernel has not yet handed over, it
    refuses new ones, and the refusal reaches the client as a transport error
    naming nothing -- it read as model failures through an afternoon of
    benchmarking before the listener was suspected. It must be a class
    attribute: TCPServer.__init__ calls listen() before the instance exists to
    set it on. The kernel clamps to net.core.somaxconn.
    """

    request_queue_size = 1024
    daemon_threads = True

STARTED = time.time()
LOG_DIR = os.environ.get("AGENT_LOG_DIR", "/logs")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8090"))
# Set from --port in main(); the page links peers on whatever port this
# agent is actually served on rather than assuming the default.
LISTEN_PORT = AGENT_PORT
MAX_BYTES = 64 * 1024

# mentat owns the model list: containers register with mentatd, and the mesh
# copies every registration to every daemon. The agent reads the local
# daemon's /status rather than keeping a registry of its own, so the two
# cannot disagree, and it needs no address to find it.
MENTAT_HTTP = os.environ.get("MENTAT_HTTP", "127.0.0.1:6380")
# Optional: the mentat-serve router, linked from the page and always let in to
# the MCP port, because it forwards every merged tool call. A router on one of
# this node's subnets is let in without it.
MENTAT_ROUTER_URL = os.environ.get("MENTAT_ROUTER_URL", "")
# How often mentatd and the engines are read. Token rates are differenced
# across this interval, so it also sets how twitchy the numbers look.
LOAD_INTERVAL_S = float(os.environ.get("LOAD_INTERVAL_S", "5"))


def cap(text: str, limit: int = MAX_BYTES) -> str:
    b = text.encode("utf-8", "replace")
    if len(b) <= limit:
        return text
    head = b[:limit].decode("utf-8", "ignore")
    return f"{head}\n\n[truncated at {limit} bytes of {len(b)}]"


def stamped(body: str, source: str) -> str:
    """Every answer says where it came from and when."""
    return (f"[{HOSTNAME} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
            f"{source}\n{body}")


# --- raw tools -------------------------------------------------------------
# One command in, its output out. Nothing between.

def t_meminfo(**_):
    return stamped(read_file("/proc/meminfo"), "cat /proc/meminfo")


def t_gpu(args: str = "", **_):
    """nvidia-smi with the caller's own flags, from a fixed set.

    --query-gpu reports N/A for memory on GB10 while --query-compute-apps
    still gives a per-pid figure, so the caller needs to pick.
    """
    allowed = {
        "": ["nvidia-smi"],
        "query": ["nvidia-smi", "--query-gpu=name,utilization.gpu,clocks.sm,"
                  "temperature.gpu,power.draw,memory.total,memory.used",
                  "--format=csv"],
        "apps": ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv"],
        "topo": ["nvidia-smi", "topo", "-m"],
        "clocks": ["nvidia-smi", "-q", "-d", "CLOCK,PERFORMANCE"],
    }
    if args not in allowed:
        return f"unknown selection {args!r}. Available: {', '.join(sorted(allowed))}"
    return stamped(cap(run(allowed[args])), " ".join(allowed[args]))


def t_processes(**_):
    return stamped(cap(run(["ps", "-eo",
                            "pid,ppid,rss,pcpu,etime,nlwp,comm,args", "--sort=-rss"])),
                   "ps -eo pid,ppid,rss,pcpu,etime,nlwp,comm,args --sort=-rss")


def t_network(**_):
    parts = [("ip -o -4 addr", run(["ip", "-o", "-4", "addr"])),
             ("ip -o link", run(["ip", "-o", "link"])),
             ("rdma link show", run(["rdma", "link", "show"]))]
    return stamped("\n".join(f"$ {c}\n{o}" for c, o in parts), "network")


def t_rdma_counters(device: str = "", **_):
    base = "/sys/class/infiniband"
    out = []
    try:
        devs = [device] if device else sorted(os.listdir(base))
    except OSError as e:
        return stamped(f"({base}: {e})", "rdma counters")
    for d in devs:
        for group in ("counters", "hw_counters"):
            p = f"{base}/{d}/ports/1/{group}"
            try:
                names = sorted(os.listdir(p))
            except OSError:
                continue
            out.append(f"# {d}/{group}")
            for n in names:
                out.append(f"{n} = {read_file(f'{p}/{n}').strip()}")
    return stamped(cap("\n".join(out) or "(no infiniband devices)"), "rdma counters")


def t_pci(**_):
    return stamped(cap(run(["lspci", "-nnk"])), "lspci -nnk")


def t_usb(**_):
    return stamped(cap(run(["lsusb", "-t"])), "lsusb -t")


def t_docker_ps(**_):
    """Container inventory, from the host's snapshot.

    Reading it from a file keeps the Docker socket out of this process. The
    socket is root-equivalent on the host, and this endpoint answers a model.
    """
    p = os.path.join(LOG_DIR, "docker-ps.log")
    try:
        age = time.time() - os.stat(p).st_mtime
    except OSError as e:
        return stamped(f"({p}: {type(e).__name__}: {e})", "docker ps snapshot")
    return stamped(cap(read_file(p)), f"docker-ps.log, {age:.0f}s old")


def t_systemd(**_):
    """Running services, from the host's snapshot.

    Same reason as t_docker_ps: this image has no systemctl, and the way to
    give it one is to mount the host's /run/systemd, which grants control and
    not only a read. So the host writes the list and this reads a file.
    """
    p = os.path.join(LOG_DIR, "systemd-units.log")
    try:
        age = time.time() - os.stat(p).st_mtime
    except OSError as e:
        return stamped(f"({p}: {type(e).__name__}: {e})", "systemd snapshot")
    return stamped(cap(read_file(p)), f"systemd-units.log, {age:.0f}s old")


def t_list_logs(**_):
    try:
        names = sorted(os.listdir(LOG_DIR))
    except OSError as e:
        return stamped(f"({LOG_DIR}: {e})", "list logs")
    rows = []
    for n in names:
        p = os.path.join(LOG_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append(f"{st.st_size:>12}  {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(st.st_mtime))}  {n}")
    return stamped("\n".join(rows) or "(empty)", f"ls -l {LOG_DIR}")


def t_read_log(file: str = "", lines: int = 200, grep: str = "", **_):
    """Tail of one file under the log directory, optionally filtered."""
    if not file:
        return "Name a file. Use list_logs to see them."
    root = os.path.realpath(LOG_DIR)
    target = os.path.realpath(os.path.join(root, file))
    if target != root and not target.startswith(root + os.sep):
        return "That path leaves the log directory."
    body = read_file(target)
    out = body.splitlines()
    if grep:
        try:
            import re
            rx = re.compile(grep)
            out = [l for l in out if rx.search(l)]
        except re.error as e:
            return f"(bad pattern: {e})"
    tail = out[-max(1, min(int(lines or 200), 5000)):]
    return stamped(cap("\n".join(tail)), f"{file} ({len(out)} matching lines)")


def _engine_pids() -> list[tuple[int, str]]:
    """(host pid, label) for the engine processes, engine core first.

    These are container processes seen through the host's /proc, which is why
    the agent can dump them without the container holding CAP_SYS_PTRACE.
    """
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for d in entries:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "EngineCore" in cmd:
            out.insert(0, (int(d), "EngineCore"))
        elif "RayWorkerProc" in cmd:
            out.append((int(d), "RayWorkerProc"))
        elif "/bin/vllm serve" in cmd:
            out.append((int(d), "APIServer"))
    return out[:4]


PY_SPY = os.environ.get("PY_SPY", "py-spy")


def t_engine_stacks(rounds: int = 3, gap_s: float = 5, **_):
    """Python stacks of the engine processes, sampled several times.

    One sample cannot tell a slow kernel from queue backpressure. CUDA
    launches are async, so when the GPU stream backs up the launch queue fills
    and whatever the CPU tries to launch next is what blocks. A frame that
    stays put across rounds is the kernel actually running. A frame that moves
    means the cause is upstream of where the sample landed.
    """
    rounds = max(1, min(int(rounds or 3), 10))
    gap_s = max(0.5, min(float(gap_s or 5), 30))
    pids = _engine_pids()
    if not pids:
        return stamped("(no engine processes found)", "engine stacks")
    out = []
    for i in range(rounds):
        if i:
            time.sleep(gap_s)
        out.append(f"----- round {i+1}/{rounds} at "
                   f"{time.strftime('%H:%M:%SZ', time.gmtime())} -----")
        for pid, label in pids:
            if label == "APIServer" and i:
                continue          # waits on the engine by construction
            out.append(f"===== {label} (host pid {pid}) =====")
            out.append(run([PY_SPY, "dump", "--pid", str(pid)], timeout=30))
    return stamped(cap("\n".join(out)), f"{PY_SPY} dump x{rounds}")


# --- tools that correlate ---------------------------------------------------

def t_memory_accounting(**_):
    """Every byte of host memory, by owner.

    Separate from meminfo because it needs three sources: /proc/meminfo,
    per-process RSS, and the GPU's per-pid figure. GPU allocations skip
    process RSS entirely, so ps accounts for about 6 GiB of 121 on a serving
    node and the total only closes once the GPU number is added.
    """
    helper = os.environ.get("SPARK_MEMORY_PY", "/usr/local/bin/spark-memory.py")
    if os.path.exists(helper):
        return stamped(cap(run(["python3", helper, "-v"], timeout=30)), helper)
    return stamped(f"({helper} not present)", "memory accounting")


def t_models(**_):
    """mentat's view of every model, with the load this agent scraped."""
    cluster = cluster_snapshot()
    with _load_lock:
        loads = dict(_load)
    return json.dumps({"router": MENTAT_ROUTER_URL or None, **cluster,
                       "load": loads}, indent=2)



# --- load sampling ----------------------------------------------------------
# The agent scrapes each engine mentat lists rather than the engine
# reporting its load. That keeps every model image out of this: a container
# announces where it serves to mentat, and what it is doing is derived here.

_load: dict[str, dict] = {}
# Why the last scrape of an engine failed, by group. An engine that answers
# /metrics is serving, so this is also what the page calls healthy.
_load_err: dict[str, str] = {}
_load_lock = threading.Lock()
_load_prev: dict[str, tuple[float, float, float]] = {}

# The names are exact and were read off a running engine. kv_cache_usage_perc
# is NOT gpu_cache_usage_perc, which is what the older status page scraped and
# which silently reports nothing on this vLLM.
_GAUGES = {
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm:kv_cache_usage_perc": "kv",
}
_COUNTERS = {
    "vllm:prompt_tokens_total": "prompt",
    "vllm:generation_tokens_total": "gen",
}


def _parse_metrics(text: str) -> dict:
    """Pull the handful of series worth showing out of Prometheus text.

    Labels are ignored beyond stripping them: one engine per container here,
    so a bare sum over duplicate series is the right reading.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        name, _, rest = line.partition("{")
        if rest:
            _, _, value = rest.partition("}")
        else:
            name, _, value = line.partition(" ")
        key = _GAUGES.get(name.strip()) or _COUNTERS.get(name.strip())
        if not key:
            continue
        try:
            out[key] = out.get(key, 0.0) + float(value.strip())
        except ValueError:
            continue
    return out




def read_interconnect() -> dict:
    """This node's RoCE rates, from the host snapshot.

    The snapshot samples two counter reads a second apart, which is the only
    way to say whether the link is busy now rather than how much it has ever
    carried. Absent on a node with no cable plugged in, which is not an error.
    """
    p = os.path.join(LOG_DIR, "interconnect.log")
    try:
        age = time.time() - os.stat(p).st_mtime
        text = read_file(p)
    except OSError:
        return {}
    if age > 300:                       # a snapshot this old describes nothing
        return {}
    # The snapshot writes: "<dev> tx <n> MB/s  rx <n> MB/s", so the units are
    # fields of their own and the rx label lands at index 4, not 3.
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[1] == "tx" and parts[4] == "rx":
            try:
                return {"dev": parts[0], "tx_mbs": float(parts[2]),
                        "rx_mbs": float(parts[5]), "age_s": round(age)}
            except ValueError:
                continue
    return {}


# How long a driver fault stays interesting. Anything older is history: these
# lines are never cleared, so without a window a single fault at boot would
# read as a permanently broken node.
DRIVER_ERR_WINDOW_S = 900


def read_driver_errors() -> dict:
    """NVRM and Xid lines from the host snapshot, counted and dated.

    Returns {} when the snapshot is missing, stale or clean. `recent` counts
    the window; `total` is the whole boot, which can be far larger.

    This exists because nothing watched it. On 2026-08-27 the driver logged
    NV_ERR_NO_MEMORY every few minutes for 23 minutes while the model answered
    requests at normal speed, and the first visible symptom was a box that had
    to be power cycled.
    """
    p = os.path.join(LOG_DIR, "driver-errors.log")
    try:
        age = time.time() - os.stat(p).st_mtime
        text = read_file(p)
    except OSError:
        return {}
    if age > 300:
        return {}
    total, recent, newest, sample = 0, 0, None, ""
    now = time.time()
    for line in text.splitlines():
        if line.startswith("# matched "):
            try:
                total = int(line.split()[2])
            except (IndexError, ValueError):
                pass
            continue
        if not line.startswith("["):
            continue
        stamp, _, msg = line[1:].partition("] ")
        try:
            # dmesg -T renders "Thu Aug 27 22:13:26 2026" in local time.
            when = time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        if newest is None or when > newest:
            newest, sample = when, msg.strip()[:200]
        if now - when <= DRIVER_ERR_WINDOW_S:
            recent += 1
    if not total and newest is None:
        return {}
    return {"total": total, "recent": recent, "sample": sample,
            "last_s": round(now - newest) if newest else None,
            "window_s": DRIVER_ERR_WINDOW_S}




# --- mentat ------------------------------------------------------------------
# mentat does the cluster work this agent used to do itself. mentatd knows the
# nodes, and mentat-serve routes the models and merges every group's MCP. The
# agent registers its own MCP endpoint as a group of one named for the node, so
# its host tools reach clients through the router, where `__group` picks the
# node.
MENTAT_GROUP = os.environ.get("MENTAT_GROUP", f"agent-{HOSTNAME}")
MENTAT_DAEMON = os.environ.get("MENTAT_DAEMON", "127.0.0.1:6379")
# Short, because one loop reads mentatd, every peer agent and every engine
# in turn, and a dead node should cost a round seconds, not minutes.
FETCH_TIMEOUT_S = 2.0
# Optional: every box in the fleet, as "name=address,..." (the name may be
# left out). mentat lists only this node's own cluster, so without this a page
# sees that cluster alone. With it, the per-box pages show these boxes, in
# this order, under these names.
FLEET = [(n if a else "", a or n) for n, _, a in (
    e.strip().partition("=") for e in os.environ.get("SPARK_FLEET", "").split(",") if e.strip())]


def register_loop():
    """Hold this agent's registration with the local daemon.

    ray.register's main() refuses a registration with no OpenAI endpoint,
    because one usually means a model that never came up. This agent has no
    model on purpose, so it calls connect() and keeps main()'s retry shape.
    """
    try:
        from ray import register
    except ImportError as e:
        print(f"mentat registration off: {e}", flush=True)
        return
    args = register.parse_args([
        "--address", MENTAT_DAEMON, "--group", MENTAT_GROUP,
        "--mcp", f"{LISTEN_PORT}/mcp", "--container", "spark-agent"])
    delay = 1.0
    while True:
        try:
            if register.connect(args) > register.SETTLED_S:
                delay = 1.0
        except (OSError, RuntimeError, ValueError) as e:
            print(f"mentat registration: {type(e).__name__}: {e}", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, register.RETRY_MAX_S)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as r:
        return json.loads(r.read().decode())


# The last complete read of the cluster: mentat's table and what each node's
# agent reported about itself.
_cluster: dict = {"mentat": None, "mentat_error": "not read yet", "nodes": {}}
_cluster_lock = threading.Lock()


def cluster_snapshot() -> dict:
    with _cluster_lock:
        return dict(_cluster)


def service_url(agent: dict, svc: dict | None) -> str | None:
    """A registered service's base URL. An empty host is the agent's node."""
    if not svc or not svc.get("port"):
        return None
    host = svc.get("host") or agent.get("node_ip") or ""
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{svc['port']}{svc.get('path') or ''}"


def head_status(st: dict) -> dict | None:
    """The mesh head's /status, or None when this daemon is the head.

    Every daemon knows every node, but the cluster's groups are held by the
    head: another daemon lists only the groups registered with it. The head
    is named by node id, and the peer table gives its address.
    """
    head = st.get("head_node_id")
    if not head or head == st.get("node_id"):
        return None
    peer = (st.get("peers") or {}).get(head) or {}
    if not peer.get("node_ip"):
        raise LookupError(f"the head {head} is not among this daemon's peers")
    port = peer.get("http_port") or MENTAT_HTTP.rsplit(":", 1)[1]
    return _get_json(f"http://{peer['node_ip']}:{port}/status")


def cluster_from_status(st: dict, head: dict | None = None) -> dict:
    """The model and daemon tables, from this daemon's /status and the head's.

    Each group is drawn from one of its agents: a live one that serves the
    OpenAI API if there is one. A model spread over several nodes registers
    that API from its head alone. The groups come from the mesh head, with
    any the head has not heard of yet from this daemon. The daemon list is
    this daemon and its peers.
    """
    groups = {}
    merged = {**(st.get("groups") or {}), **((head or {}).get("groups") or {})}
    for name, g in merged.items():
        agents = [a for a in (g.get("agents") or {}).values() if a.get("services")]
        if not agents:
            continue
        a = max(agents, key=lambda a: (bool(a.get("alive")) and not a.get("degraded"),
                                       "openai" in a["services"]))
        groups[name] = {
            "openai": service_url(a, a["services"].get("openai")),
            "mcp": service_url(a, a["services"].get("mcp")),
            "why_not": (None if a.get("alive") and not a.get("degraded") else
                        "degraded" if a.get("alive") else "agent gone"),
        }
    daemons = {}
    port = MENTAT_HTTP.rsplit(":", 1)[1]
    for n in [dict(st, alive=True), *(st.get("peers") or {}).values()]:
        ip = n.get("node_ip")
        if not ip:
            continue
        p = n.get("http_port") or port
        daemons[f"{ip}:{p}"] = {
            "alternates": [f"{a}:{p}" for a in n.get("addrs") or [] if a != ip],
            "connected": bool(n.get("alive")),
            "error": None if n.get("alive") else "not heard from",
        }
    return {"groups": groups, "daemons": daemons}


def read_nodes(daemons: dict) -> dict[str, dict]:
    """address -> that node's agent status, or why there is none.

    mentat knows nothing of the interlink or the driver, so each node's agent
    is asked for its own. The mentat daemons carry `connected`; the boxes of
    the fleet list carry their `rank` in it and their name.
    """
    out = {}
    for addr, d in sorted(daemons.items()):
        ip = addr.rsplit(":", 1)[0]
        out[addr] = {"ip": ip, "addrs": [ip] + [a.rsplit(":", 1)[0]
                                                for a in d.get("alternates") or []],
                     "connected": bool(d.get("connected")),
                     "daemon_error": d.get("error")}
    for rank, (name, ip) in enumerate(FLEET):
        out[f"fleet:{ip}"] = {"ip": ip, "addrs": [ip], "rank": rank, "alias": name}

    def fetch(entry):
        try:
            entry["agent"] = _get_json(f"http://{entry['ip']}:{LISTEN_PORT}/status.json")
        except Exception as e:
            entry["agent_error"] = f"{type(e).__name__}: {e}"

    # At the same time, so a box that does not answer delays none of the others.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(fetch, out.values()))
    return out


def view() -> dict:
    """Everything a panel draws from: see panels/__init__.py."""
    snap = cluster_snapshot()
    with _load_lock:
        load, load_err = dict(_load), dict(_load_err)
    return {"host": HOSTNAME, "port": LISTEN_PORT, "router": MENTAT_ROUTER_URL,
            "mentat": snap["mentat"], "mentat_error": snap["mentat_error"],
            "nodes": snap["nodes"], "load": load, "load_err": load_err}


# Each panel's last sample() of this node, by panel name, served in
# /status.json for the other nodes' pages.
_samples: dict[str, dict] = {}


def sample_loop():
    while True:
        for p in panels.PANELS:
            if hasattr(p, "sample"):
                try:
                    _samples[panels.name(p)] = p.sample(view())
                except Exception as e:
                    _samples[panels.name(p)] = {"error": f"{type(e).__name__}: {e}"}
        time.sleep(panels.SAMPLE_S)


def watch_cluster():
    """Read mentatd and every node's agent, then scrape each engine.

    Token rates come from differencing the totals, because vLLM publishes
    counters and a counter says nothing about now.
    """
    while True:
        try:
            st = _get_json(f"http://{MENTAT_HTTP}/status")
            try:
                head, err = head_status(st), None
            except Exception as e:
                head, err = None, f"mesh head: {type(e).__name__}: {e}"
            mentat = cluster_from_status(st, head)
        except Exception as e:
            mentat, err = None, f"{type(e).__name__}: {e}"
        nodes = read_nodes((mentat or {}).get("daemons") or {})
        with _cluster_lock:
            _cluster.update(mentat=mentat, mentat_error=err, nodes=nodes)
        groups = (mentat or {}).get("groups") or {}
        for name, g in groups.items():
            if g.get("openai"):
                sample_load(name, g["openai"])
        with _load_lock:
            for gone in set(_load) - set(groups):
                _load.pop(gone, None)
            for gone in set(_load_err) - set(groups):
                _load_err.pop(gone, None)
        time.sleep(LOAD_INTERVAL_S)


def sample_load(name: str, url: str):
    parts = urllib.parse.urlsplit(url)
    try:
        with urllib.request.urlopen(
                f"{parts.scheme}://{parts.netloc}/metrics",
                timeout=FETCH_TIMEOUT_S) as r:
            m = _parse_metrics(r.read().decode("utf-8", "replace"))
    except Exception as e:
        with _load_lock:
            _load.pop(name, None)
            _load_err[name] = f"no /metrics: {type(e).__name__}"
        _load_prev.pop(name, None)
        return
    now = time.time()
    entry = {"running": int(m.get("running", 0)),
             "waiting": int(m.get("waiting", 0)),
             "kv": round(m.get("kv", 0.0) * 100, 1)}
    prev = _load_prev.get(name)
    if prev:
        dt = now - prev[0]
        # A restarted engine resets its counters, so a negative delta is a
        # new engine rather than a rate. Drop it and re-baseline.
        if dt >= 1.0:
            dp, dg = m.get("prompt", 0.0) - prev[1], m.get("gen", 0.0) - prev[2]
            if dp >= 0 and dg >= 0:
                entry["prompt_tps"] = round(dp / dt, 1)
                entry["gen_tps"] = round(dg / dt, 1)
    _load_prev[name] = (now, m.get("prompt", 0.0), m.get("gen", 0.0))
    with _load_lock:
        _load[name] = entry
        _load_err.pop(name, None)



TOOLS = {
    "meminfo": (t_meminfo, "Raw /proc/meminfo.", {}),
    "gpu": (t_gpu, "nvidia-smi output. Pick a view with args: '' for the "
            "default table, 'query' for a csv of utilisation/clocks/temp/power, "
            "'apps' for per-pid GPU memory, 'topo' for the topology matrix, "
            "'clocks' for clock and performance detail. On GB10 --query-gpu "
            "reports N/A for memory while 'apps' still answers.",
            {"args": {"type": "string"}}),
    "processes": (t_processes, "Raw ps, sorted by RSS. GPU allocations do not "
                  "appear here.", {}),
    "network": (t_network, "Raw ip addr, ip link and rdma link output.", {}),
    "rdma_counters": (t_rdma_counters, "Every RoCE counter for a port, raw. "
                      "Rates need two reads: these are lifetime totals.",
                      {"device": {"type": "string"}}),
    "pci": (t_pci, "Raw lspci -nnk, including the bound driver.", {}),
    "usb": (t_usb, "Raw lsusb -t.", {}),
    "docker_ps": (t_docker_ps, "Raw docker ps -a.", {}),
    "systemd_units": (t_systemd, "Running systemd services.", {}),
    "list_logs": (t_list_logs, "Files in the log directory with size and mtime.", {}),
    "read_log": (t_read_log, "Tail of one log file, optionally filtered by a "
                 "regex. Capped at 64 KB.",
                 {"file": {"type": "string"}, "lines": {"type": "integer"},
                  "grep": {"type": "string"}}),
    "memory_accounting": (t_memory_accounting, "Host memory by owner, "
                          "reconciled against MemTotal. Needs /proc/meminfo, "
                          "per-process RSS and the GPU's per-pid figure "
                          "together, which is why it is not raw meminfo.", {}),
    "engine_stacks": (t_engine_stacks, "Python stacks of the engine processes, "
                      "sampled over several rounds. A frame that stays put is "
                      "the kernel running; one that moves is queue "
                      "backpressure, since an async CUDA launch blocks on "
                      "whatever it tries to launch next. Reads container "
                      "processes through the host's /proc.",
                      {"rounds": {"type": "integer"},
                       "gap_s": {"type": "number"}}),
    "models": (t_models, "mentat's table of every group, model and node, read "
               "from the mesh head, with the token load this agent scraped "
               "from each engine.", {}),
    **panels.tools(),
}




# --- who may ask ------------------------------------------------------------
# The network is the only control: the endpoint answers loopback, this node's
# own subnets and the router. Docker bridge ranges are absent on purpose, so a
# container on this host cannot read host state just by being here. A
# bridge-networked client still reaches the agent when it dials a real host
# address, because Docker masquerades the source to the host's own IP on that
# route.
#
# ALLOWED_SOURCES replaces the subnets: comma-separated CIDR blocks or address
# prefixes ("192.168.1."). Loopback and the router stay allowed either way.
ALLOWED_SOURCES = [p.strip() for p in os.environ.get(
    "ALLOWED_SOURCES", "").split(",") if p.strip()]
ROUTER_HOST = urllib.parse.urlsplit(MENTAT_ROUTER_URL).hostname or ""
# Interfaces whose subnets are never let in: container bridges and veths.
_BRIDGE_PREFIXES = ("docker", "br-", "veth", "cni", "flannel", "virbr")
# A fabric cable plugged in later brings a new subnet, so they are re-read.
SUBNETS_TTL_S = 30.0
_subnets: tuple[float, list] = (0.0, [])
_subnets_lock = threading.Lock()


def local_subnets() -> list:
    """This node's IPv4 and IPv6 subnets, from `ip -j addr`, without bridges."""
    global _subnets
    with _subnets_lock:
        if time.time() - _subnets[0] < SUBNETS_TTL_S:
            return _subnets[1]
        nets = []
        try:
            out = subprocess.run(["ip", "-j", "addr"], capture_output=True,
                                 text=True, timeout=5).stdout
            for link in json.loads(out or "[]"):
                if link.get("ifname", "").startswith(_BRIDGE_PREFIXES):
                    continue
                for a in link.get("addr_info") or []:
                    try:
                        nets.append(ipaddress.ip_interface(
                            f"{a['local']}/{a['prefixlen']}").network)
                    except (KeyError, ValueError):
                        pass
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            print(f"local subnets: {type(e).__name__}: {e}", flush=True)
        _subnets = (time.time(), nets)
        return nets


def _matches(addr, ip, rule: str) -> bool:
    if "/" in rule:
        try:
            return ip is not None and ip in ipaddress.ip_network(rule, strict=False)
        except ValueError:
            return False
    return addr.startswith(rule)


def source_allowed(addr: str) -> bool:
    addr = addr.removeprefix("::ffff:")
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        ip = None
    if addr == ROUTER_HOST or (ip is not None and ip.is_loopback):
        return True
    if ALLOWED_SOURCES:
        return any(_matches(addr, ip, r) for r in ALLOWED_SOURCES)
    return ip is not None and any(ip in n for n in local_subnets())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _deny_source(self) -> bool:
        addr = self.client_address[0]
        if source_allowed(addr):
            return False
        self._send(403, b'{"error":"source not permitted"}\n', "application/json")
        return True

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._deny_source():
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        panel = panels.by_path(path)
        if panel:
            self._send(200, panels.render(panel, view()), "text/html; charset=utf-8")
            return
        if path in ("/healthz", "/status.json"):
            # Peer agents read this for the node table, so it carries this
            # node's interlink and driver figures.
            self._send(200, json.dumps({
                "node": HOSTNAME, "uptime_s": round(time.time() - STARTED, 1),
                "group": MENTAT_GROUP, "tools": sorted(TOOLS),
                "interconnect": read_interconnect(),
                "driver": read_driver_errors(),
                **_samples,
            }, indent=2).encode(), "application/json")
            return
        self._send(404, b"not found\n", "text/plain")

    def do_POST(self):
        if self._deny_source():
            return
        path = self.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        panel = panels.by_path(path)
        if panel and hasattr(panel, "post"):
            msg = panel.post(urllib.parse.parse_qs(raw.decode(errors="replace")))
            self._send(200, panels.render(panel, view(), msg), "text/html; charset=utf-8")
            return
        try:
            payload = json.loads(raw.decode() or "{}")
        except ValueError:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        if path == "/mcp":
            self._send(200, json.dumps(mcp(payload)).encode(), "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")


def mcp(req: dict) -> dict:
    rid, method = req.get("id"), req.get("method")
    def ok(result): return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": "2024-11-05",
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": f"spark-agent@{HOSTNAME}",
                                  "version": "0.2"}})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d,
                              "inputSchema": {"type": "object",
                                              "properties": dict(p)}}
                             for n, (_, d, p) in sorted(TOOLS.items())]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in TOOLS:
            return ok({"content": [{"type": "text",
                                    "text": f"no tool {name!r} on {HOSTNAME}"}],
                       "isError": True})
        try:
            text = TOOLS[name][0](**(params.get("arguments") or {}))
        except Exception as e:
            return ok({"content": [{"type": "text",
                                    "text": f"{type(e).__name__}: {e}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": text}], "isError": False})
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"no method {method!r}"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    global LISTEN_PORT
    LISTEN_PORT = a.port
    threading.Thread(target=watch_cluster, daemon=True).start()
    threading.Thread(target=register_loop, daemon=True).start()
    threading.Thread(target=sample_loop, daemon=True).start()
    srv = Server((a.bind, a.port), Handler)
    print(f"spark-agent on {a.bind}:{a.port} as {HOSTNAME}, group "
          f"{MENTAT_GROUP}, {len(TOOLS)} tools, logs at {LOG_DIR}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
