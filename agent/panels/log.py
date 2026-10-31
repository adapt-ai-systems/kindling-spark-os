"""Log: one timeline of fleet events, kept across reboots.

Each agent appends its own events to EVENTS, one JSON line each, and serves
them at /log.json. Spark OS / is RAM, so the file is on the DGX OS disk, which
the image mounts at /run/sparkos/host. The page reads every box's /log.json at
once and merges them by time, newest at the bottom.

Any module records an event with event(text, src, state, kind). This one
records boots (and the image they came up on) and agent restarts; Health
records check changes and Power the caps set from the page or the MCP tool.
Events older than KEEP_DAYS are dropped when the agent starts.
"""
import concurrent.futures
import json
import os
import threading
import time

from .common import HOSTNAME, boxes, esc, get_json, image, mk, read_file, uptime

PATH, TITLE = "/log", "Log"
REFRESH_S = 30
EVENTS = os.environ.get("AGENT_EVENTS", "/run/sparkos/host/var/lib/spark-agent/events.jsonl")
KEEP_DAYS = 90
DAYS = 3                                # shown by default; "Earlier" adds a week
# state -> the marker's class. "you" is a command sent from the page or MCP.
MARK = {"fail": "bad", "warn": "warn", "on": "on"}

_lock = threading.Lock()
_error = ""                             # why the last write failed, if it did
_started = False


def event(text: str, src: str = "", state: str = "info", kind: str = "", t: float = 0, **extra):
    """Append one event to this node's log. Never raises: a full or missing
    disk leaves the reason on the page instead."""
    global _error
    rec = {"t": round(t or time.time(), 1), "host": HOSTNAME, "src": src, "state": state,
           "kind": kind, "text": text, **extra}
    with _lock:
        try:
            with open(EVENTS, "a") as f:
                f.write(json.dumps(rec) + "\n")
            _error = ""
        except OSError as e:
            _error = f"{EVENTS}: {type(e).__name__}: {e}"


def read(since: float = 0) -> list[dict]:
    """This node's events from `since` on, oldest first."""
    out = []
    try:
        with open(EVENTS) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue            # a line cut short by a crash
                if rec.get("t", 0) >= since:
                    out.append(rec)
    except OSError:
        pass
    return out


def _start():
    """Once per agent start: trim old events, then record a boot or a restart."""
    keep = read(time.time() - KEEP_DAYS * 86400)
    with _lock:
        try:
            tmp = EVENTS + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(json.dumps(r) + "\n" for r in keep)
            os.replace(tmp, EVENTS)
        except OSError:
            pass                        # event() below reports why
    boot_id = read_file("/proc/sys/kernel/random/boot_id").strip()
    img = image()
    last = next((r for r in reversed(keep) if r.get("kind") == "boot"), {})
    if last.get("boot_id") == boot_id:
        event("Agent restarted.", "agent", "info", "start")
        return
    text = f"Booted Spark OS {img or '(version unknown)'}."
    if last.get("image") and last["image"] != img:
        text += f" It ran {last['image']} before."
    # Stamped at the time the box came up, not when the agent did.
    event(text, "boot", "on", "boot", t=time.time() - uptime(), boot_id=boot_id, image=img)


def sample(view: dict) -> dict:
    global _started
    if not _started:
        _started = True
        _start()
    return {"path": EVENTS, "error": _error}


def _days(view: dict) -> int:
    try:
        return max(1, min(KEEP_DAYS, int((view.get("query") or {}).get("days", [DAYS])[0])))
    except ValueError:
        return DAYS


def data(view: dict) -> dict:
    return {"host": HOSTNAME, "error": _error,
            "events": read(time.time() - _days(view) * 86400)}


def fleet_events(view: dict, days: int, only: list | None = None) -> tuple[list, list]:
    """(events, notes) from every box, or the boxes in `only`, merged by time.
    Each event carries "box", the box's name on the page."""
    bx = only or boxes(view)

    def fetch(b):
        if b["self"]:
            return b, data({"query": {"days": [days]}}), None
        try:
            return b, get_json(f"http://{b['ip']}:{view['port']}{PATH}.json?days={days}", 3), None
        except Exception as e:
            return b, None, f"{type(e).__name__}: {e}"

    events, why = [], {}                 # reason -> the boxes it holds for
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for b, got, err in ex.map(fetch, bx):
            if err:
                why.setdefault("no events: its agent predates the Log" if "404" in err
                               else f"no events: {err}", []).append(b["name"])
                continue
            if got.get("error"):
                why.setdefault(f"cannot save events: {got['error']}", []).append(b["name"])
            events += [{**r, "box": b["name"]} for r in got.get("events") or []]
    events.sort(key=lambda r: r["t"])
    return events, [f"{', '.join(names)}: {reason}" for reason, names in why.items()]


def when(t: float) -> str:
    return time.strftime("%-I:%M %p", time.localtime(t))


def line(r: dict) -> str:
    src = esc(r["box"] + (f" · {r['src']}" if r.get("src") else ""))
    cmd = r.get("state") == "you"
    marker = "" if cmd else mk(MARK.get(r.get("state"), ""))
    tx = f"<span class=c-bad>{esc(r['text'])}</span>" if r.get("state") == "fail" else esc(r["text"])
    return (f"<div class=\"ln{' cmd' if cmd else ''}\"><div class=tm>{when(r['t'])}</div><div>"
            f"<div class=src>{marker}{src}</div><div class=tx>{tx}</div></div></div>")


def page(view: dict, msg: str = "") -> str:
    days = _days(view)
    events, notes = fleet_events(view, days)
    body = ""
    today = time.strftime("%a %b %-d")
    by_day: dict[str, list] = {}
    for r in events:
        by_day.setdefault(time.strftime("%a %b %-d", time.localtime(r["t"])), []).append(r)
    for day, rs in by_day.items():
        label = f"Today &middot; {day}" if day == today else day
        body += (f"<div class=day>{label}<span>{len(rs)} event{'s' * (len(rs) != 1)}</span></div>"
                 + "".join(line(r) for r in rs))
    note = "".join(f"<div class=lead>{esc(n)}</div>" for n in notes)
    more = (f"<a class=more href=\"{PATH}?days={min(days + 7, KEEP_DAYS)}\">&uarr; <b>Earlier</b>"
            f"<span>last {days} day{'s' * (days != 1)} shown &middot; kept {KEEP_DAYS} days across reboots</span></a>")
    return f"""<div class=vh><h3>Log</h3><span>{len(events)} events from {len(boxes(view))} boxes
&middot; newest at the bottom &middot; times {time.strftime('%Z')}</span></div>{note}
<div class=lg id=lg>{more}{body or '<div class=lead>no events yet</div>'}</div>
<script>var l=document.getElementById('lg');l.scrollTop=l.scrollHeight</script>"""
