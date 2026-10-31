"""Cluster: mentat's nodes and the models they serve.

Every daemon holds the same cluster, so any node can draw it and there is no
page that only one node can serve.
"""
from .common import DASH, esc, node_of

PATH, TITLE = "/", "Cluster"
REFRESH_S = 10


def _driver_cell(dv: dict) -> str:
    # A fault inside the window is the alarm. Older ones stay visible but dim,
    # because the count never resets short of a reboot.
    if dv.get("recent"):
        return (f"<span class=c-bad title=\"{esc(dv.get('sample') or '')}\">"
                f"{dv['recent']} in {dv.get('window_s', 900) // 60}m</span>")
    if dv.get("total"):
        return f"<span class=dim>{dv['total']} earlier</span>"
    return DASH


def page(view: dict, msg: str = "") -> str:
    groups = (view.get("mentat") or {}).get("groups") or {}
    loads, load_errs = view["load"], view["load_err"]
    nodes = [n for n in view["nodes"].values() if "connected" in n]

    where = node_of(view)

    served: dict[str, int] = {}
    for g in groups.values():
        if g.get("openai"):
            n = where(g["openai"])
            served[n] = served.get(n, 0) + 1

    rows = []
    for n in nodes:
        agent = n.get("agent") or {}
        name = agent.get("node") or n["ip"]
        link = f"<a href=\"http://{esc(n['ip'])}:{view['port']}/\">{esc(name)}</a>"
        if name == view["host"]:
            link += "<span class=tag>this node</span>"
        mentat = ("connected" if n["connected"] else
                  f"<span class=c-bad>{esc(n.get('daemon_error') or 'down')}</span>")
        if "agent" in n:
            ic = agent.get("interconnect") or {}
            ics = f"{ic['tx_mbs']:.0f}/{ic['rx_mbs']:.0f} MB/s" if ic else DASH
            drv = _driver_cell(agent.get("driver") or {})
        else:
            ics = f"<span class=c-bad title=\"{esc(n.get('agent_error'))}\">no agent</span>"
            drv = DASH
        rows.append(f"<tr><td class=n>{link}</td><td class=hx>{esc(', '.join(n['addrs']))}</td>"
                    f"<td>{mentat}</td><td>{ics}</td><td>{drv}</td>"
                    f"<td class=r>{served.get(name, 0)}</td></tr>")

    models = []
    for name, g in sorted(groups.items()):
        if not g.get("openai"):
            continue                    # agents and other MCP-only groups
        why_not = g.get("why_not") or load_errs.get(name)
        healthy = not why_not and name in loads
        ld = loads.get(name) or {}
        if "gen_tps" in ld:
            # Prompt and generation are separate on purpose: a long prefill
            # shows as prompt tokens with generation at zero, which is the
            # difference between a busy engine and a stalled one.
            tps = f"{ld['prompt_tps']:.0f} in / {ld['gen_tps']:.0f} out"
        elif ld:
            tps = "<span class=dim>warming</span>"
        else:
            tps = DASH
        q = f"{ld['running']}/{ld['waiting']}" if ld else DASH
        kv = f"{ld['kv']:.0f}%" if ld else DASH
        state = ("healthy" if healthy else
                 f"<span class=c-bad>{esc(why_not or 'not read yet')}</span>")
        models.append(f"<tr><td class=n>{esc(', '.join(g.get('models') or [name]))}</td>"
                      f"<td>{esc(name)}</td><td>{esc(where(g['openai']))}</td>"
                      f"<td>{state}</td><td>{tps}</td><td>{q}</td><td>{kv}</td>"
                      f"<td class='dim hx'>{esc(g['openai'])}</td></tr>")

    err = view.get("mentat_error")
    note = f"<div class=msg>mentatd unreachable: {esc(err)}</div>" if err else ""
    empty = "mentatd unreachable" if err else "none registered"
    router = (f"routed by <a href=\"{esc(view['router'])}/\">{esc(view['router'])}</a>"
              if view.get("router") else "from this node's mentatd")
    return f"""<div class=vh><h3>Cluster</h3><span>{router}</span></div>{note}
<div class=sec><span class=lbl>Nodes</span></div>
<div class=scroll><table class=tb><tr><th>Node</th><th class=hx>Addresses</th><th>mentatd</th>
<th>Interlink tx/rx</th><th>Driver faults</th><th class=r>Models</th></tr>
{''.join(rows) or '<tr><td colspan=6 class=dim>no daemons known</td></tr>'}</table></div>
<div class=sec style="border-top:1px solid var(--line)"><span class=lbl>Models</span></div>
<div class=scroll><table class=tb><tr><th>Model</th><th>Group</th><th>Node</th><th>State</th>
<th>Tokens/s</th><th>Run/wait</th><th>KV</th><th class=hx>Serving</th></tr>
{''.join(models) or f'<tr><td colspan=8 class=dim>{empty}</td></tr>'}</table></div>"""
