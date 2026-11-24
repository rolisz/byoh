"""The one screen the human sees: what was done, what was asked, and every draft with the reviewer's flags.

Static for now (buttons don't post anywhere). The live-demo version will need a tiny server.
"""
import json
from html import escape
from pathlib import Path

from fakemail import Mailbox
from tools import MailTools


def write_review_page(box: Mailbox, tools: MailTools, report: dict, summary: str) -> Path:
    msgs = box.msgs.values()
    inbox = [m for m in msgs if "INBOX" in m["labelIds"]]
    archived = [m for m in msgs if "INBOX" not in m["labelIds"]]
    asks_path = box.dir / "asks.jsonl"
    asks = [json.loads(x) for x in asks_path.read_text().splitlines()] if asks_path.exists() else []

    cards = ""
    for t, r in report.items():
        d, rv = r["draft"], r.get("review") or {}
        flags = "".join(f'<li class="commit">Commitment: {escape(c)}</li>' for c in rv.get("commitments", []))
        flags += "".join(f'<li class="leak">Possible leak: {escape(x)}</li>' for x in r["leaks"] + rv.get("leaks", []))
        if rv and not rv.get("recipient_ok", True):
            flags += '<li class="leak">Reviewer: recipients look wrong</li>'
        if rv.get("note"):
            flags += f'<li>{escape(rv["note"])}</li>'
        cards += (f'<section class="draft"><div class="to">To: {escape(", ".join(d["to"]))}</div>'
                  f'<div class="subj">{escape(d["subject"])}</div><pre>{escape(d["body"])}</pre>'
                  f'<ul class="flags">{flags or "<li class=ok>No flags</li>"}</ul>'
                  '<div class="btns"><button>Send</button><button>Edit</button><button>Discard</button></div></section>')

    ask_rows = "".join(f"<li><b>{escape(a['kind'])}</b>: {escape(a['question'])} &rarr; {escape(a['answer'])}</li>"
                       for a in asks) or "<li>None</li>"
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Inbox review</title><style>
body{{font:15px/1.45 system-ui,sans-serif;max-width:820px;margin:32px auto;color:#222;background:#f6f5f1}}
h1{{font-size:22px}} .stats span{{display:inline-block;margin-right:18px;font-weight:600}}
.draft{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin:14px 0}}
.to{{color:#666;font-size:13px}} .subj{{font-weight:600;margin:2px 0 8px}}
pre{{white-space:pre-wrap;font:inherit;background:#fafafa;padding:10px;border-radius:6px}}
.flags li.commit{{color:#9a5b00}} .flags li.leak{{color:#b00020;font-weight:600}} .flags li.ok{{color:#2e7d32}}
button{{margin-right:8px;padding:6px 14px;border-radius:6px;border:1px solid #bbb;background:#fff}}
details{{margin-top:20px}}</style></head><body>
<h1>Your inbox, reviewed</h1>
<div class="stats"><span>{len(archived)} archived</span><span>{len(inbox)} left in inbox</span>
<span>{len(report)} drafts to review</span><span>{len(asks)} questions asked</span></div>
<h2>Drafts</h2>{cards or "<p>No drafts.</p>"}
<h2>What the agent asked you</h2><ul>{ask_rows}</ul>
<details><summary>Agent's summary</summary><pre>{escape(summary)}</pre></details>
</body></html>"""
    path = box.dir / "review.html"
    path.write_text(html)
    return path
