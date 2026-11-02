"""Fleet: one rail per box for GPU and board temperature, power, clock and disk. A box with
a failing check flashes red; tap a box to open it on the Box page.

Each rail takes the colour of its reading's place in the range (common.ramp),
so a box that runs hot or full stands out. The note under the name is the
box's worst problem, else its clock cap.
"""
from . import health
from .common import boxes, esc, mk, ramp

PATH, TITLE = "/", "Fleet"
REFRESH_S = 10
# label, the sample and key it reads, its format, its range. A range of None
# runs to the GPU's own maximum clock.
RAILS = [("Temp", "power", "temp_c", "{:.0f}&deg;C", (20, 90)),
         ("Board", "box", "board_c", "{:.0f}&deg;C", (20, 105)),   # hottest ACPI zone; its critical trip is 104.8 C
         ("Power", "power", "watts", "{:.1f} W", (0, 100)),
         ("Clock", "power", "sm_mhz", "{:.0f}", None),
         ("Disk", "box", "disk_pct", "{:.0f}%", (0, 100))]
MARK = {"fail": "bad", "warn": "warn"}


def worst(res: list) -> tuple[str, str, str]:
    """(state, label, text) of a box's worst check, or ("ok", "", "")."""
    bad = [r for r in res if r[1] in ("fail", "warn")]
    if not bad:
        return "ok", "", ""
    label, state, text = max(bad, key=lambda r: health.WORST[r[1]])
    return state, label, text


def rail(label: str, v, lo: float, hi: float, fmt: str) -> str:
    """One labelled reading over a dotted bar, both in the reading's range colour."""
    if not isinstance(v, (int, float)):
        return (f"<div class=m><div class=r><span class=lbl>{label}</span><span class=\"num dim\">-</span>"
                f"</div><div class=sg></div></div>")
    c = ramp(v, lo, hi)
    pct = max(2, min(100, round((v - lo) * 100 / ((hi - lo) or 1))))
    return (f"<div class=m><div class=r><span class=lbl>{label}</span><span class=num style=\"color:{c}\">"
            f"{fmt.format(v)}</span></div><div class=sg><i style=\"width:{pct}%;color:{c}\"></i></div></div>")


def rails(st: dict) -> str:
    max_mhz = (st.get("power") or {}).get("max_mhz") or 3003
    return "".join(rail(label, (st.get(part) or {}).get(key), *(rng or (0, max_mhz)), fmt)
                   for label, part, key, fmt, rng in RAILS)


def page(view: dict, msg: str = "") -> str:
    rows, capped = "", 0
    for box, res in health.fleet(view):
        st = box["status"] or {}
        state, label, text = worst(res)
        cap = (st.get("power") or {}).get("cap_mhz") or 0
        capped += bool(cap)
        if state != "ok":
            note = f"<small class=c-{'bad' if state == 'fail' else 'warn'}>{esc(label)} &middot; {esc(text)}</small>"
        elif cap:
            note = f"<small class=c-lime>cap {cap} MHz</small>"
        else:
            note = "<small>no cap</small>"
        tag = "<span class=tag>this node</span>" if box["self"] else ""
        rows += (f"<a class=\"rw{' bad' if state == 'fail' else ''}\" href=\"/box?name={esc(box['name'])}\">"
                 f"<div class=nm><b>{mk(MARK.get(state, ''))}{esc(box['name'])}{tag}</b>{note}</div>"
                 f"{rails(st)}<span class=go>&rsaquo;</span></a>")
    n = len(boxes(view))
    return f"""<div class=vh><h3>Fleet</h3><span>{n} boxes &middot; {capped} capped &middot; tap a box to open it</span></div>
<div class=rl>{rows or '<div class=lead>no boxes known</div>'}</div>
<div class=foot><span>Color = place in range</span><span class=ramp></span><span>low &rarr; high</span>
<span>temp 20&ndash;90 &deg;C &middot; power 0&ndash;100 W &middot; clock 0&ndash;max MHz &middot; disk 0&ndash;100%</span></div>"""
