"""Health: every box against every check, on one page. The goal is all green.

Each node runs the checks in checks.py on itself and publishes the results in
/status.json under "health". Any node's page draws the whole fleet from those,
so a box that cannot reach another still shows what that box says of itself.
"""
import json
import time

from . import checks, log
from .common import boxes, esc, mk

PATH, TITLE = "/health", "Health"
REFRESH_S = 10
STALE_S = 60                            # results older than this mean the sampler is stuck
# UP is judged by the page, not by the box: an agent that does not answer
# cannot say so itself.
COLUMNS = [("UP", "agent answers")] + [(label, about) for label, _, about in checks.CHECKS]
WORST = {"fail": 3, "warn": 2, "ok": 1, "na": 0}

HOLD = 2                                # samples a new state must last before the Log records it

_last_view: dict = {}
_logged: dict | None = None             # label -> the state last recorded in the Log
_held: dict = {}                        # label -> (new state, samples seen in it)


def sample(view: dict) -> dict:
    """This node's results, and the fabric addresses other nodes probe."""
    _last_view.update(view)
    facts = checks.read(view)
    results = []
    for label, check, _ in checks.CHECKS:
        try:
            state, text = check(facts)
        except Exception as e:
            state, text = "fail", f"the check broke: {type(e).__name__}: {e}"
        results.append([label, state, text])
    _log_changes(results)
    fabric = [{"netdev": link["netdev"], "addr": a} for link in facts["links"] for a in link["addrs"]]
    return {"at": facts["now"], "fabric": fabric, "checks": results}


def _log_changes(results: list):
    """Record each check that changed state in the Log, once the new state
    has lasted HOLD samples, so a check that flickers does not fill it. After
    a restart the last states come from the Log, so a lasting fault is not
    recorded twice."""
    global _logged
    if _logged is None:
        _logged = {r["src"]: r["state"] for r in log.read() if r.get("kind") == "check"}
    for label, state, text in results:
        if state == "na" or state == _logged.get(label, "ok"):
            _held.pop(label, None)
            continue
        was, n = _held.get(label, (state, 0))
        n = n + 1 if was == state else 1
        _held[label] = (state, n)
        if n >= HOLD:
            _logged[label] = state
            _held.pop(label)
            log.event(f"{'Back to ok' if state == 'ok' else state.capitalize()}: {text}",
                      label, state, "check")


def results(box: dict) -> list:
    """[(label, state, text)] for one box, one per column."""
    st = box["status"]
    if not st:
        return [("UP", "fail", f"agent does not answer: {box['error'] or 'no reply'}")] + [
            (label, "na", "no reply from the agent") for label, _ in COLUMNS[1:]]
    h = st.get("health")
    if not h:
        why, state = "no results: the agent just started, or it predates Health", "na"
    elif "error" in h:
        why, state = f"the checks did not run: {h['error']}", "fail"
    else:
        got = {label: (state, text) for label, state, text in h["checks"]}
        age = time.time() - h["at"]
        up = (("warn", f"results are {age:.0f}s old: the agent's sampler is stuck") if age > STALE_S
              else ("ok", f"agent answers, running for {st.get('uptime_s', 0) / 3600:.1f} h"))
        return [("UP", *up)] + [(label, *got.get(label, ("na", "not checked by this agent")))
                                for label, _ in COLUMNS[1:]]
    return [("UP", "ok", "agent answers")] + [(label, state, why) for label, _ in COLUMNS[1:]]


def fleet(view: dict) -> list:
    return [(box, results(box)) for box in boxes(view)]


def alarms(view: dict) -> list:
    return [(box["name"], label, text) for box, res in fleet(view)
            for label, state, text in res if state == "fail"]


def t_health(**_):
    if not _last_view:
        return json.dumps({"error": "no sample yet"})
    return json.dumps({box["name"]: {label: [state, text] for label, state, text in res}
                       for box, res in fleet(_last_view)}, indent=2)


TOOLS = {
    "health": (t_health, "Every box against every health check: per box, per check, "
               "[state, what was seen]. State is ok, warn, fail, or na.", {}),
}


def page(view: dict, msg: str = "") -> str:
    rows = fleet(view)
    states = [state for _, res in rows for _, state, _ in res]
    count = {s: states.count(s) for s in WORST}
    about = dict(COLUMNS)
    head = "<tr><th></th>" + "".join(f"<th>{esc(label)}</th>" for label, _ in COLUMNS) + "<th></th></tr>"
    matrix, problems = "", []
    for box, res in rows:
        worst = max(res, key=lambda r: WORST[r[1]])[1]
        judged = [state for _, state, _ in res if state != "na"]
        ok = judged.count("ok")
        cells = "".join(f"<td class=\"c {state}\" title=\"{esc(box['name'])} {esc(label)}: "
                        f"{esc(about[label])}. {esc(text)}\"></td>" for label, state, text in res)
        link = f"<a href=\"http://{esc(box['ip'])}:{view['port']}{PATH}\">{esc(box['name'])}</a>"
        matrix += (f"<tr><td class=n>{mk({'fail': 'bad', 'warn': 'warn'}.get(worst, ''))}{link}</td>{cells}"
                   f"<td class=\"sc{' full' if ok == len(judged) else ''}\">{ok}/{len(judged)}</td></tr>")
        problems += [(state, box["name"], label, text) for label, state, text in res
                     if state in ("fail", "warn")]
    problems.sort(key=lambda p: -WORST[p[0]])
    table = "".join(f"<tr><td class=n>{mk('bad' if s == 'fail' else 'warn')}{esc(b)}</td>"
                    f"<td>{esc(label)}</td><td class=t>{esc(text)}</td></tr>"
                    for s, b, label, text in problems)
    na = f" &middot; {count['na']} n/a" if count["na"] else ""
    warn = f"<span class=c-warn>{count['warn']} warn</span>" if count["warn"] else "0 warn"
    fail = f"<span class=c-bad>{count['fail']} fail</span>" if count["fail"] else "0 fail"
    legend = "".join(f"<span><i class=\"k {k}\"></i>{t}</span>" for k, t in
                     (("ok", "pass"), ("warn", "warn"), ("fail", "fail"), ("", "n/a")))
    legend += "".join(f"<span><b>{esc(label)}</b> {esc(text)}</span>" for label, text in COLUMNS)
    return f"""<div class=vh><h3>Health</h3><span>goal: all green &middot; {len(states)} checks &middot;
{count['ok']} pass &middot; {warn} &middot; {fail}{na}</span></div>
<div class="hm scroll"><table>{head}{matrix}</table></div>
<div class=scroll><table class=tb><tr><th>Box</th><th>Check</th><th>Detail</th></tr>
{table or '<tr><td colspan=3 class=dim>no problems: all green</td></tr>'}</table></div>
<div class=foot>{legend}</div>"""
