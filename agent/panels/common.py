"""What the agent and its panels share: running commands, reading files, and
the page shell every tab is drawn in."""
import os
import shutil
import socket
import subprocess
import time

HOSTNAME = socket.gethostname()
CSS = open(os.path.join(os.path.dirname(__file__), "style.css")).read()
DASH = "<span class=dim>-</span>"


def run(cmd: list[str], timeout: float = 15) -> str:
    """Combined stdout and stderr, or a line saying why there is none."""
    if not shutil.which(cmd[0]):
        return f"({cmd[0]} is not installed on {HOSTNAME})"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"({' '.join(cmd)} timed out after {timeout}s)"
    except OSError as e:
        return f"({' '.join(cmd)} failed: {type(e).__name__}: {e})"
    return (r.stdout or "") + (r.stderr or "")


def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        return f"({path}: {type(e).__name__}: {e})"


def esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def mk(state: str = "") -> str:
    """The small square that marks a box's state: gray, lime, amber or red."""
    return f"<i class=\"mk {state}\"></i>"


def boxes(view: dict) -> list[dict]:
    """One entry per box: name, host, ip, self, status (its /status.json or None)
    and error.

    With a fleet list (SPARK_FLEET) the boxes are those, in its order. Without
    one they are mentat's nodes, which mentat can list twice under different
    addresses, so they are merged by host name.
    """
    nodes = list(view["nodes"].values())
    fleet = sorted((n for n in nodes if "rank" in n), key=lambda n: n["rank"])
    out: dict[str, dict] = {}
    for n in fleet or nodes:
        st = n.get("agent")
        host = (st or {}).get("node") or ""
        key = n.get("alias") or host or n["ip"]
        if key in out and out[key]["status"]:
            continue
        out[key] = {"name": key, "host": host, "ip": n["ip"], "status": st,
                    "self": host == view["host"], "error": n.get("agent_error")}
    return list(out.values())


def shell(title: str, nav: list, active: str, alarms: list, body: str,
          host: str, refresh_s: int = 0) -> str:
    links = "".join(f"<a href=\"{p}\"{' class=on' if p == active else ''}>{esc(t)}</a>"
                    for p, t in nav)
    if alarms:
        box, what, _ = alarms[0]
        state = (f"<span class=c-bad>{len(alarms)} fail &middot; {esc(box)} "
                 f"{esc(what)}</span>{mk('bad')}")
    else:
        state = f"<span class=dim>all clear</span>{mk('on')}"
    strip = "".join(
        f"<div class=ab><span class=lbl>Fail</span><span class=x>{esc(b)} &middot; {esc(w)}</span>"
        f"<span class=d>{esc(t)}</span><a href=\"/health\">Open &rarr;</a></div>"
        for b, w, t in alarms[:3])
    if len(alarms) > 3:
        strip += (f"<div class=ab><span class=lbl>Fail</span><span class=x>"
                  f"{len(alarms) - 3} more</span><a href=\"/health\">Open &rarr;</a></div>")
    meta = f"<meta http-equiv=refresh content={refresh_s}>" if refresh_s else ""
    seen = time.strftime("%H:%M:%S", time.localtime())
    return f"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">{meta}
<title>{esc(title)} &middot; {esc(host)}</title><style>{CSS}</style>
<div class=wrap><div class=app>
<div class=top><span class=brand>{mk('on')}Spark OS</span><nav>{links}</nav><span class=st>{state}</span></div>
{strip}{body}
</div><footer>seen from {esc(host)} at {seen}{f' &middot; refreshes every {refresh_s}s' if refresh_s else ''}
&middot; <a href="/status.json">status.json</a></footer></div>"""
