"""Power: the GPU clock cap.

GB10 has no power limit: nvidia-smi reports every limit as N/A. The box's
power is held down by capping the SM clock instead. The agent only writes the
wanted cap to CAP_FILE; sparkos-gpucap (root, started by a .path unit) applies
it and leaves its result in CAP_STATUS. /etc/spark is copied from the DGX OS
disk at boot, so a cap persists.
"""
import json
import os
import time

from . import log
from .common import DASH, boxes, esc, run

PATH, TITLE = "/power", "Power"
CAP_FILE = "/etc/spark/gpucap/mhz"
CAP_STATUS = "/run/spark/gpucap.status"


def sample(view=None) -> dict:
    """This node's live clock, load and the cap as wanted and as applied."""
    out: dict = {"cap_mhz": 0, "applied": None}
    q = run(["nvidia-smi", "--query-gpu=clocks.sm,clocks.max.sm,utilization.gpu,"
             "power.draw,temperature.gpu", "--format=csv,noheader,nounits"], 10)
    parts = [x.strip() for x in q.strip().splitlines()[-1].split(",")] if q.strip() else []
    if len(parts) == 5 and not q.startswith("error"):
        def num(v):
            try:
                return float(v)
            except ValueError:
                return None
        out.update(sm_mhz=num(parts[0]), max_mhz=num(parts[1]), util_pct=num(parts[2]),
                   watts=num(parts[3]), temp_c=num(parts[4]))
    else:
        out["error"] = q.strip()[:200]
    try:
        with open(CAP_FILE) as f:
            out["cap_mhz"] = int("".join(c for c in f.read() if c.isdigit()) or 0)
    except OSError:
        pass
    try:
        with open(CAP_STATUS) as f:
            st = json.load(f)
        out["applied"] = bool(st.get("applied")) and st.get("cap_mhz") == out["cap_mhz"]
        out["applied_at"] = st.get("at")
        if st.get("error"):
            out["apply_error"] = st["error"]
    except (OSError, ValueError):
        pass
    return out


def set_cap(mhz: int, by: str) -> dict:
    """Write the wanted cap (0 clears it), wait briefly for the root unit, and
    record both the command and its outcome in the Log."""
    p = _set_cap(int(mhz))
    log.event(f"cap {mhz or 'clear'}", by, "you", "cmd")
    if p.get("apply_error"):
        log.event(f"Cap {mhz} MHz did not apply: {p['apply_error']}", "cap", "fail", "cap")
    elif not p.get("applied"):
        log.event(f"Cap {mhz} MHz written; sparkos-gpucap has not applied it yet.", "cap", "warn", "cap")
    else:
        clock = f" The clock reads {p['sm_mhz']:.0f} MHz." if isinstance(p.get("sm_mhz"), float) else ""
        log.event((f"Cap {mhz} MHz applied." if mhz else "Cap cleared.") + clock, "cap", "on", "cap")
    return p


def _set_cap(mhz: int) -> dict:
    if mhz < 0:
        raise ValueError("cap must be 0 (clear) or a positive MHz figure")
    mx = sample().get("max_mhz")
    if mhz and mx and mhz > mx:
        raise ValueError(f"cap {mhz} MHz is above this GPU's maximum {mx:.0f}")
    if mhz and mhz < 100:
        raise ValueError("cap below 100 MHz refused")
    tmp = CAP_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{mhz}\n")
    os.replace(tmp, CAP_FILE)         # one inotify event for the .path unit
    deadline = time.time() + 5
    while time.time() < deadline:
        p = sample()
        if p.get("applied") is not None and p.get("applied_at") and p["cap_mhz"] == mhz:
            if p["applied"] or p.get("apply_error"):
                return p
        time.sleep(0.3)
    return sample()


def post(form: dict) -> str:
    want = 0 if form.get("clear") else int((form.get("cap") or ["0"])[0] or 0)
    try:
        set_cap(want, "page")
    except (ValueError, OSError) as e:
        return str(e)
    return ""


def t_power_cap(mhz: int = -1, **_):
    if mhz is None or int(mhz) < 0:
        return json.dumps(sample(), indent=2)
    try:
        return json.dumps(set_cap(int(mhz), "mcp"), indent=2)
    except (ValueError, OSError) as e:
        return json.dumps({"error": str(e), **sample()}, indent=2)


TOOLS = {
    "power_cap": (t_power_cap, "This node's GPU clock, load, draw and clock cap. "
                  "With mhz: set the cap (0 clears it); GB10 has no power limit, "
                  "so the SM clock cap is the power cap. Persists across reboots.",
                  {"mhz": {"type": "integer"}}),
}


def _fmt(p: dict, k: str, fmt: str = "{:.0f}") -> str:
    v = p.get(k)
    return fmt.format(v) if isinstance(v, (int, float)) else DASH


def _cap_cell(p: dict) -> str:
    cap = p.get("cap_mhz") or 0
    if not cap:
        return "<span class=dim>none</span>"
    if p.get("applied"):
        return f"<span class=c-lime>{cap}</span>"
    return f"{cap} <span class=c-bad>{esc(p.get('apply_error') or 'not applied')}</span>"


def _row(b: dict, port: int) -> str:
    link = f"<a href=\"http://{esc(b['ip'])}:{port}/power\">{esc(b['name'])}</a>"
    if b["self"]:
        link += "<span class=tag>this node</span>"
    p = (b["status"] or {}).get("power")
    if not p:
        return f"<tr><td class=n>{link}</td><td colspan=5><span class=c-bad>no agent</span></td></tr>"
    util = p.get("util_pct")
    utilcell = (f"<span class=hb><i class=\"{'warn' if util >= 90 else ''}\" "
                f"style=\"width:{util:.0f}%\"></i></span>{util:.0f}%"
                if isinstance(util, (int, float)) else DASH)
    return (f"<tr><td class=n>{link}</td><td>{_fmt(p, 'sm_mhz')} <span class=dim>/ "
            f"{_fmt(p, 'max_mhz')}</span></td><td>{utilcell}</td>"
            f"<td class=r>{_fmt(p, 'watts', '{:.1f}')} W</td><td class=r>{_fmt(p, 'temp_c')}&deg;</td>"
            f"<td class=r>{_cap_cell(p)}</td></tr>")


def cap_form(action: str, cap: int, mx: int, label: str = "cap this node") -> str:
    """The set and clear form. Its action may be another box's /power."""
    return f"""<form class=cap method=post action="{esc(action)}">
<label for=cap>{esc(label)}</label><input id=cap type=number name=cap min=100 max={mx} step=1
value="{cap or ''}" placeholder="none"><label>MHz, max {mx}</label>
<button type=submit class=p>Set cap</button><button type=submit name=clear value=1>Clear</button>
</form>"""


def page(view: dict, msg: str = "") -> str:
    me = sample()
    mx = int(me.get("max_mhz") or 3000)
    cap = me.get("cap_mhz") or 0
    rows = "".join(_row(b, view["port"]) for b in boxes(view))
    note = f"<div class=msg>{esc(msg)}</div>" if msg else ""
    return f"""<div class=vh><h3>Power</h3><span>cap = the SM clock's maximum &middot; survives a reboot</span></div>
<p class=lead>GB10 has no power limit, so the cap is the SM clock's maximum. The live clock sits next to the cap,
so a cap that did not take shows.</p>{note}
<div class=stats>
<div><b>{_fmt(me, 'sm_mhz')}</b><small>SM MHz of {mx}</small></div>
<div><b>{_fmt(me, 'util_pct')}%</b><small>util</small></div>
<div><b>{_fmt(me, 'watts', '{:.1f}')} W</b><small>draw</small></div>
<div><b>{_fmt(me, 'temp_c')}&deg;</b><small>temp</small></div>
<div><b>{cap or 'none'}</b><small>cap</small></div>
</div>
{cap_form("/power", cap, mx)}
<div class=scroll><table class=tb><tr><th>Box</th><th>SM clock / max</th><th>Util</th>
<th class=r>Draw</th><th class=r>Temp</th><th class=r>Cap</th></tr>
{rows or '<tr><td colspan=6 class=dim>no boxes known</td></tr>'}</table></div>"""
