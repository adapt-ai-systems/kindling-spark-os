"""The agent's pages, one module per tab.

To add a tab, write a module in this directory and add its name to ORDER. A
module defines:

    PATH, TITLE      the URL it serves and the label on its tab
    page(view, msg)  the HTML body of the page

and, only if it needs them:

    sample(view)     this node's own reading, taken every SAMPLE_S by the
                     agent's sampler thread and published in /status.json under
                     the module's name. Every other node's page reads it there.
    post(form)       handle the page's form. Returns a message for the page.
    alarms(view)     [(box, what, text)] for failures. Every page shows them in
                     a red strip, whichever tab found them.
    REFRESH_S        reload the page this often. Leave it out on a page with a
                     form, so the reload cannot wipe what is being typed.
    TOOLS            MCP tools, {name: (function, description, params)}.

`view` is what the agent knows when the page is drawn: this node, every box
and its last /status.json, mentat's groups and the engine load. The agent's
view() builds it, and common.boxes() turns it into one entry per box.
"""
import importlib

from . import common

ORDER = ["cluster", "power"]
PANELS = [importlib.import_module(f"{__name__}.{name}") for name in ORDER]
# How often each panel's sample() runs.
SAMPLE_S = 10.0


def by_path(path: str):
    return next((p for p in PANELS if p.PATH == path), None)


def tools() -> dict:
    return {name: t for p in PANELS for name, t in getattr(p, "TOOLS", {}).items()}


def render(panel, view: dict, msg: str = "") -> bytes:
    alarms = [a for p in PANELS if hasattr(p, "alarms") for a in p.alarms(view)]
    nav = [(p.PATH, p.TITLE) for p in PANELS]
    return common.shell(panel.TITLE, nav, panel.PATH, alarms, panel.page(view, msg),
                        view["host"], getattr(panel, "REFRESH_S", 0)).encode()


def name(panel) -> str:
    return panel.__name__.rsplit(".", 1)[1]
