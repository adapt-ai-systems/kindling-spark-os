"""Box: one box in full, opened from the Fleet page (/box?name=dgx2).

What the Fleet rails cannot show: every check with what it saw, the fabric
ports and their far ends, what the box serves, its clock cap with a form that
sets it on that box, its recent events, and its image. Every part comes from
the box's own /status.json and /log.json, so any node's page can open any box.
"""
import glob
import os
import time

from . import cables, checks, health, log, power
from .common import DASH, boxes, esc, image, meminfo, mk, node_of, ramp, uptime
from .fleet import MARK, worst

PATH, TITLE = "/box", "Box"
PARENT = "/"
REFRESH_S = 30                          # long enough to type a cap
RECENT = 8                              # events shown


NAMES = {"acpitz": "Board", "nvme": "NVMe", "mlx5": "NIC", "mt7925_phy0": "Wi-Fi"}


def _read(path: str):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _milli(v):
    """A sysfs millidegree reading as degrees; None when absent or not a sane limit (NVMe sensors 1-2 report 65261 C)."""
    return int(v) / 1000 if v and v.lstrip("-").isdigit() and int(v) < 200000 else None


def temps() -> list:
    """Every temperature sensor the box has, [name, degrees C, the limit the device itself reports or None]: the board's
    ACPI thermal zones (critical trip point), and each hwmon chip other than acpitz (crit, else max): NVMe, the ConnectX
    NIC, Wi-Fi. The GPU's own reading is in the power sample."""
    out = []
    base = "/sys/class/thermal"
    for z in sorted(glob.glob(f"{base}/thermal_zone*"), key=lambda z: int(z.rsplit("e", 1)[1])):
        c = _milli(_read(f"{z}/temp"))
        crit = next((_milli(_read(t[:-5] + "_temp")) for t in sorted(glob.glob(f"{z}/trip_point_*_type"))
                     if _read(t) == "critical"), None)
        if c is not None:
            out.append([f"Board zone {z.rsplit('e', 1)[1]}", c, crit])
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=lambda h: int(h.rsplit("n", 1)[1])):
        name = _read(f"{h}/name") or "?"
        if name == "acpitz":
            continue
        for f in sorted(glob.glob(f"{h}/temp*_input")):
            p = f[:-6]
            c = _milli(_read(f))
            if c is None:
                continue
            label = _read(f"{p}_label") or ""
            dev = os.path.basename(os.path.realpath(f"{h}/device")) if name == "mlx5" else ""
            out.append([" ".join(x for x in (NAMES.get(name, name), label if label != "asic" else "", dev) if x), c,
                        _milli(_read(f"{p}_crit")) or _milli(_read(f"{p}_max"))])
    return out


def sample(view: dict) -> dict:
    """What only this page and the Fleet rails need: image, boot, disk, memory, every temperature sensor."""
    mount, pct, tb = checks._disk()
    mem = meminfo()
    t = temps()
    return {"temps": t, "board_c": max((c for n, c, _ in t if n.startswith("Board")), default=None),
            "image": image(), "kernel": os.uname().release,
            "boot_at": time.time() - uptime(), "disk_mount": mount, "disk_pct": pct, "disk_tb": tb,
            "mem_avail_gib": mem.get("MemAvailable", 0) / 2**20,
            "mem_total_gib": mem.get("MemTotal", 0) / 2**20}


def _kv(rows: list) -> str:
    return "".join(f"<div><span>{esc(a)}</span><span>{b}</span></div>" for a, b in rows)


def _ago(s: float) -> str:
    return f"{s / 86400:.1f} d" if s > 86400 else f"{s / 3600:.1f} h" if s > 3600 else f"{s / 60:.0f} min"


def _now(st: dict) -> str:
    p, b = st.get("power") or {}, st.get("box") or {}
    out = ""
    for v, lo, hi, fmt in ((p.get("temp_c"), 20, 90, "{:.0f}&deg;C"), (p.get("watts"), 0, 100, "{:.1f} W"),
                           (p.get("sm_mhz"), 0, p.get("max_mhz") or 3003, "{:.0f} MHz"),
                           (b.get("disk_pct"), 0, 100, "{:.0f}% disk")):
        if isinstance(v, (int, float)):
            out += f"<span style=\"color:{ramp(v, lo, hi)}\">{fmt.format(v)}</span>"
    return out


def _temps(st: dict) -> str:
    """GPU first, then every other sensor, each coloured against its own limit (20 C to the limit, else 20 to 90 C)."""
    g = (st.get("power") or {}).get("temp_c")
    rows = ([["GPU", g, None]] if isinstance(g, (int, float)) else []) + ((st.get("box") or {}).get("temps") or [])
    if not rows:
        return "<div class=dim>no reading</div>"
    return "".join(f"<div><span>{esc(n)}</span><span><span class=num style=\"color:{ramp(c, 20, lim or 90)}\">{c:.0f}&deg;C</span>"
                   f"{f' <small class=dim>limit {lim:.0f}&deg;</small>' if lim else ''}</span></div>" for n, c, lim in rows)


def _checks(res: list) -> str:
    about = dict(health.COLUMNS)
    return "".join(f"<div>{mk(MARK.get(state, '') if state != 'na' else '')}<span title=\"{esc(about[label])}\">"
                   f"{esc(label)}</span><em{' class=c-bad' if state == 'fail' else ' class=c-warn' if state == 'warn' else ''}>"
                   f"{esc(text)}</em></div>" for label, state, text in res)


def _ports(view: dict, name: str) -> str:
    rows = ""
    for c in cables.fleet(view)[0]:
        ends = [e for e in (c["a"], c["b"]) if e]
        mine = next((e for e in ends if e["box"] == name), None)
        if not mine:
            continue
        far = next((e for e in ends if e is not mine), None)
        r = mine["reading"] or {}
        errs = sum((r.get("since_boot") or {}).get(k, 0) for k in checks.PHY_FAIL)
        speed = f"{r['speed'] // 1000}G" if r.get("speed") else DASH
        rows += (f"<tr><td>{mk(MARK.get(c['state'], ''))} {esc(mine['port'])}</td>"
                 f"<td class=hx>{esc(', '.join(r.get('netdevs') or []))}</td><td>{speed}</td>"
                 f"<td>{r.get('mtu') or DASH}</td>"
                 f"<td>{esc(far['box'] + ' ' + far['port']) if far else DASH}</td><td class=r>{errs}</td></tr>"
                 + (f"<tr><td colspan=6 class=c-{'bad' if c['state'] == 'fail' else 'warn'}>{esc(c['text'])}</td></tr>"
                    if c["state"] != "ok" else ""))
    return rows


def _side(bx: list, fleet: dict, box: dict) -> str:
    """The list of boxes down the left, each with its live clock."""
    out = ""
    for b in bx:
        clk = ((b["status"] or {}).get("power") or {}).get("sm_mhz")
        out += (f"<a href=\"{PATH}?name={esc(b['name'])}\"{' class=sel' if b is box else ''}>"
                f"{mk(MARK.get(worst(fleet[b['name']])[0], ''))}{esc(b['name'])}"
                f"<small>{f'{clk:.0f}' if isinstance(clk, (int, float)) else ''}</small></a>")
    return out


def _serving(view: dict, host: str) -> list:
    where = node_of(view)
    groups = (view.get("mentat") or {}).get("groups") or {}
    out = []
    for name, g in sorted(groups.items()):
        if g.get("openai") and where(g["openai"]) == host:
            ld = view["load"].get(name) or {}
            tps = f" &middot; {ld['gen_tps']:.0f} tok/s out" if "gen_tps" in ld else ""
            out.append((", ".join(g.get("models") or [name]), f"{esc(name)}{tps}"))
    return out or [("Models", "<span class=dim>none</span>")]


def page(view: dict, msg: str = "") -> str:
    bx = boxes(view)
    want = ((view.get("query") or {}).get("name") or [""])[0]
    box = (next((b for b in bx if b["name"] == want), None)
           or next((b for b in bx if b["self"]), None) or (bx[0] if bx else None))
    if not box:
        return "<div class=vh><h3>Box</h3></div><div class=lead>no boxes known</div>"
    port = view["port"]
    fleet = {b["name"]: res for b, res in health.fleet(view)}
    st = box["status"] or {}
    res = fleet[box["name"]]
    side = _side(bx, fleet, box)
    judged = [s for _, s, _ in res if s != "na"]
    p, sysd = st.get("power") or {}, st.get("box") or {}
    events, _ = log.fleet_events(view, 30, [box])
    ev = "".join(f"<div><span class=tm>{log.when(r['t'])}</span><span>{esc(r['text'])}</span></div>"
                 for r in reversed(events[-RECENT:])) or "<div class=dim>no events</div>"
    mx = int(p.get("max_mhz") or 3003)
    cap = p.get("cap_mhz") or 0
    capnote = (f"<span class=\"num c-lime\">{cap} MHz</span>" if cap and p.get("applied") else
               f"<span class=\"num c-bad\">{cap} MHz, {esc(p.get('apply_error') or 'not applied')}</span>" if cap
               else "<span class=\"num dim\">none</span>")
    system = [(k, esc(sysd[key]) if sysd.get(key) else DASH) for k, key in (("Image", "image"), ("Kernel", "kernel"))]
    if sysd.get("boot_at"):
        system.append(("Up", f"{_ago(time.time() - sysd['boot_at'])} &middot; since "
                             f"{time.strftime('%b %-d %-I:%M %p', time.localtime(sysd['boot_at']))}"))
    if sysd.get("mem_total_gib"):
        system.append(("Memory", f"{sysd['mem_avail_gib']:.0f} of {sysd['mem_total_gib']:.0f} GiB free"))
    if sysd.get("disk_tb"):
        system.append(("Disk", f"{sysd['disk_pct']:.0f}% of {sysd['disk_tb']:.1f} TB &middot; {esc(sysd['disk_mount'])}"))
    system.append(("Agent", f":{port} &middot; " + (f"up {_ago(st['uptime_s'])}" if st else
                                                    f"<span class=c-bad>{esc(box['error'] or 'no reply')}</span>")))
    ports = _ports(view, box["name"])
    note = f"<div class=msg>{esc(msg)}</div>" if msg else ""
    return f"""<div class=vh><h3>Box</h3><span>{esc(box['name'])}</span><a class=bk href="/">&larr; Fleet</a></div>{note}
<div class=bx><div class=ls>{side}</div><div class=dt>
<div class=hd><div><h3>{esc(box['name'])}</h3><div class=meta>{esc(box['host'] or '?')} &middot; {esc(box['ip'])}</div></div>
<div class=now>{_now(st)}</div></div>
<div class=g><div>
<div class=bk><span class=lbl>Checks<span>{judged.count('ok')} of {len(judged)} ok</span></span><div class=ckl>{_checks(res)}</div></div>
<div class=bk><span class=lbl>Fabric ports</span><div class=scroll><table class=pt><tr><th>Port</th><th class=hx>Netdevs</th>
<th>Speed</th><th>MTU</th><th>Far end</th><th class=r>Errors</th></tr>
{ports or '<tr><td colspan=6 class=dim>no cable reading</td></tr>'}</table></div></div>
</div><div>
<div class=bk><span class=lbl>Serving</span><div class=kv>{_kv(_serving(view, box['host']))}</div></div>
<div class=bk><div class=ch><span class=lbl>GPU clock cap</span>{capnote}</div>
{power.cap_form(f"http://{box['ip']}:{port}/power", cap, mx, f"cap {box['name']}")}</div>
<div class=bk><span class=lbl>Recent on {esc(box['name'])}<a href="/log">Log &rarr;</a></span><div class=ev>{ev}</div></div>
<div class=bk><span class=lbl>Temperatures</span><div class=kv>{_temps(st)}</div></div>
<div class=bk><span class=lbl>System</span><div class=kv>{_kv(system)}</div></div>
</div></div></div></div>"""
