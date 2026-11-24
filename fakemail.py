"""A fake, local, Gmail-shaped mailbox for the talk #3 demo. Nothing ever leaves the machine.

Every run gets its own copy of inbox/ under runs/<name>/, and everything is logged there:
  mailbox.json      the final state (labels, archive, trash)
  actions.jsonl     every API call the agent made, with params
  outbox.jsonl      every "sent" email (they go nowhere)
  drafts.jsonl      every draft
  filters.jsonl     every filter created
  model_calls.jsonl usage + timing of every model call (written by the harness via log_model_call)

This is the "world", not the harness: it stays the same in every step of the talk.
"""
import json
import re
import shutil
import time
from datetime import datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).parent
FIXTURE = ROOT / "inbox"

# The generic tool people plug in first: one thin wrapper over the whole mail API.
MAIL_API_TOOL = {
    "name": "mail_api",
    "description": (
        "Call the user's mailbox API (Gmail-style REST, user is always 'me'). Methods and params:\n"
        "- users.messages.list {q?, labelIds?, maxResults?, pageToken?} -> message ids\n"
        "- users.messages.get {id, format?: 'full'|'metadata'}\n"
        "- users.messages.modify {id, addLabelIds?, removeLabelIds?}\n"
        "- users.messages.batchModify {ids, addLabelIds?, removeLabelIds?}\n"
        "- users.messages.trash {id} / users.messages.delete {id} / users.messages.batchDelete {ids}\n"
        "- users.messages.send {to, subject, body, cc?, threadId?}\n"
        "- users.drafts.create {to, subject, body, cc?, threadId?}\n"
        "- users.threads.get {id}\n"
        "- users.labels.list {} / users.labels.create {name}\n"
        "- users.settings.filters.create {criteria: {from?, subject?, query?}, action: {addLabelIds?, removeLabelIds?}}\n"
        "Archiving a message = removing the INBOX label."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "API method, e.g. users.messages.list"},
            "params": {"type": "object", "description": "Method parameters"},
        },
        "required": ["method"],
    },
}


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", "", html))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class Mailbox:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        data = json.loads((run_dir / "mailbox.json").read_text())
        self.owner = data["owner"]
        self.msgs = {m["id"]: m for m in data["messages"]}
        self.labels = {"INBOX", "UNREAD", "SENT", "DRAFT", "TRASH", "SPAM", "IMPORTANT", "STARRED"}
        for m in self.msgs.values():
            self.labels.update(m["labelIds"])
        self.config = json.loads((FIXTURE / "config.json").read_text())
        self.today = datetime.fromisoformat(self.config["now"]).strftime("%A %d %B %Y, %H:%M")
        self.t0 = time.time()

    @classmethod
    def fresh(cls, name: str | None = None) -> "Mailbox":
        name = name or datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = ROOT / "runs" / name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        shutil.copy(FIXTURE / "mailbox.json", run_dir / "mailbox.json")
        return cls(run_dir)

    # ---------------------------------------------------------------- logging
    def _log(self, file: str, record: dict):
        record = {"t": round(time.time() - self.t0, 2), **record}
        with open(self.dir / file, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _save(self):
        data = {"owner": self.owner, "messages": list(self.msgs.values())}
        (self.dir / "mailbox.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))

    def log_model_call(self, response, seconds: float, model: str | None = None):
        u = response.usage
        self._log("model_calls.jsonl", {
            "model": model or response.model,
            "seconds": round(seconds, 2),
            "stop_reason": response.stop_reason,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "tool_calls": sum(1 for b in response.content if b.type == "tool_use"),
        })

    # ---------------------------------------------------------------- the API
    def call(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        fn = getattr(self, "_" + method.replace("users.", "").replace(".", "_"), None)
        try:
            if fn is None:
                raise ValueError(f"Unknown method {method}")
            result = fn(**params)
            ok = True
        except Exception as e:  # the world reports errors like an API would
            result = {"error": {"code": 400, "message": f"{type(e).__name__}: {e}"}}
            ok = False
        self._log("actions.jsonl", {"method": method, "params": params, "ok": ok,
                                    "result_chars": len(json.dumps(result, ensure_ascii=False))})
        return result

    def _get_msg(self, id):
        if id not in self.msgs:
            raise KeyError(f"Message {id} not found")
        return self.msgs[id]

    def _messages_list(self, q: str = "", labelIds=None, maxResults: int = 100, pageToken: str | None = None, **_):
        found = sorted(self.msgs.values(), key=lambda m: m["id"], reverse=True)
        for lab in labelIds or []:
            found = [m for m in found if lab in m["labelIds"]]
        for tok in (q or "").split():
            if tok == "is:unread":
                found = [m for m in found if "UNREAD" in m["labelIds"]]
            elif tok.startswith("in:") or tok.startswith("label:"):
                lab = tok.split(":", 1)[1].upper()
                found = [m for m in found if lab in [x.upper() for x in m["labelIds"]]]
            elif tok.startswith("from:"):
                found = [m for m in found if tok[5:].lower() in m["from"].lower()]
            elif tok.startswith("subject:"):
                found = [m for m in found if tok[8:].lower() in m["subject"].lower()]
            else:
                found = [m for m in found if tok.lower() in (m["subject"] + m["html"]).lower()]
        if "TRASH" not in (labelIds or []) and "in:trash" not in (q or ""):
            found = [m for m in found if "TRASH" not in m["labelIds"]]
        start = int(pageToken or 0)
        page = found[start:start + int(maxResults)]
        out = {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page],
               "resultSizeEstimate": len(found)}
        if start + int(maxResults) < len(found):
            out["nextPageToken"] = str(start + int(maxResults))
        return out

    def _messages_get(self, id, format: str = "full", **_):
        m = self._get_msg(id)
        headers = [{"name": k, "value": m[k.lower()]} for k in ("From", "To", "Date", "Subject")]
        out = {"id": m["id"], "threadId": m["threadId"], "labelIds": m["labelIds"],
               "snippet": html_to_text(m["html"])[:200], "payload": {"headers": headers}}
        if format != "metadata":
            out["payload"]["mimeType"] = "text/html"
            out["payload"]["body"] = {"data": m["html"]}
        return out

    def _threads_get(self, id, **_):
        msgs = [self._messages_get(m["id"]) for m in self.msgs.values() if m["threadId"] == id]
        if not msgs:
            raise KeyError(f"Thread {id} not found")
        return {"id": id, "messages": msgs}

    def _messages_modify(self, id, addLabelIds=None, removeLabelIds=None, **_):
        m = self._get_msg(id)
        for lab in addLabelIds or []:
            if lab not in self.labels:
                raise ValueError(f"Label {lab} does not exist; create it first")
            if lab not in m["labelIds"]:
                m["labelIds"].append(lab)
        m["labelIds"] = [x for x in m["labelIds"] if x not in (removeLabelIds or [])]
        self._save()
        return {"id": id, "labelIds": m["labelIds"]}

    def _messages_batchModify(self, ids, addLabelIds=None, removeLabelIds=None, **_):
        for i in ids:
            self._messages_modify(i, addLabelIds, removeLabelIds)
        return {}

    def _messages_trash(self, id, **_):
        return self._messages_modify(id, ["TRASH"], ["INBOX"])

    def _messages_delete(self, id, **_):
        self._get_msg(id)
        del self.msgs[id]
        self._save()
        return {}

    def _messages_batchDelete(self, ids, **_):
        for i in ids:
            self._messages_delete(i)
        return {}

    def _send(self, file, to, subject, body, cc=None, threadId=None, **_):
        rec = {"to": to, "cc": cc, "subject": subject, "body": body, "threadId": threadId}
        self._log(file, rec)
        self._seq = getattr(self, "_seq", 0) + 1  # unique per mailbox (a clock-based id collided within one batch)
        return {"id": f"{file[0]}{self._seq:04d}", "threadId": threadId}

    def _messages_send(self, **kw):
        return self._send("outbox.jsonl", **kw)

    def _drafts_create(self, **kw):
        kw = kw.get("message", kw)  # tolerate Gmail's {message: {...}} nesting
        return self._send("drafts.jsonl", **kw)

    def _labels_list(self, **_):
        return {"labels": [{"id": x, "name": x} for x in sorted(self.labels)]}

    def _labels_create(self, name, **_):
        self.labels.add(name)
        return {"id": name, "name": name}

    def _settings_filters_create(self, criteria=None, action=None, **_):
        self._log("filters.jsonl", {"criteria": criteria, "action": action})
        return {"id": f"f{len(self.labels)}", "criteria": criteria, "action": action}
