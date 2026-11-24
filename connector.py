"""What people actually plug in first: a Gmail-connector-style toolset (steps 0 and 1).

Modelled on the tool names of a typical Gmail connector / MCP server. Already split into read and write tools,
and still including the irreversible ones: send, reply, forward, trash. Runs against the same fake mailbox.
"""
import re

from fakemail import Mailbox, html_to_text


def _tool(name, description, props, required=()):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": list(required)}}


_S, _I = {"type": "string"}, {"type": "integer"}
_LIST = {"type": "array", "items": {"type": "string"}}

CONNECTOR_TOOLS = [
    _tool("search_threads", "Search the mailbox with Gmail query syntax (e.g. 'in:inbox', 'is:unread', 'from:x'). "
          "Returns threads with message ids, sender, subject, date and a snippet.",
          {"query": _S, "max_results": _I, "page_token": _S}),
    _tool("get_message", "Get one message: headers, labels and body.", {"message_id": _S}, ["message_id"]),
    _tool("get_thread", "Get every message in a thread, with bodies.", {"thread_id": _S}, ["thread_id"]),
    _tool("list_labels", "List the mailbox's labels.", {}),
    _tool("create_label", "Create a label.", {"name": _S}, ["name"]),
    _tool("update_message_labels", "Add and/or remove labels on messages. Archive = remove INBOX.",
          {"message_ids": _LIST, "add_labels": _LIST, "remove_labels": _LIST}, ["message_ids"]),
    _tool("create_draft", "Save a draft.", {"to": _S, "subject": _S, "body": _S, "thread_id": _S}, ["to", "subject", "body"]),
    _tool("send_message", "Send an email.", {"to": _S, "subject": _S, "body": _S, "cc": _S, "thread_id": _S},
          ["to", "subject", "body"]),
    _tool("reply", "Reply to a message (sends immediately).", {"message_id": _S, "body": _S, "reply_all": {"type": "boolean"}},
          ["message_id", "body"]),
    _tool("forward", "Forward a message to someone (sends immediately).", {"message_id": _S, "to": _S, "note": _S},
          ["message_id", "to"]),
    _tool("trash_message", "Move a message to the trash.", {"message_id": _S}, ["message_id"]),
]


def _addr(s: str) -> str:
    m = re.search(r"<([^>]+)>", s)
    return (m.group(1) if m else s).strip()


class Connector:
    def __init__(self, box: Mailbox):
        self.box = box
        self.drafts: dict[str, dict] = {}  # latest draft per thread, for the harness's checks and review page

    def run(self, name: str, args: dict):
        fn = getattr(self, name, None)
        if fn is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return fn(**args)
        except Exception as e:  # report like an API would
            return {"error": f"{type(e).__name__}: {e}"}

    def search_threads(self, query: str = "", max_results: int = 20, page_token: str | None = None):
        res = self.box.call("users.messages.list", {"q": query, "maxResults": 500})
        if "error" in res:
            return res
        threads: dict[str, list] = {}
        for ref in res["messages"]:
            threads.setdefault(ref["threadId"], []).append(self.box.msgs[ref["id"]])
        items = list(threads.items())
        start = int(page_token or 0)
        out = []
        for tid, msgs in items[start:start + int(max_results)]:
            last = msgs[0]
            out.append({"thread_id": tid, "message_ids": [m["id"] for m in msgs], "from": last["from"],
                        "subject": last["subject"], "date": last["date"], "labels": last["labelIds"],
                        "snippet": html_to_text(last["html"])[:160]})
        page = {"threads": out, "total_threads": len(items)}
        if start + int(max_results) < len(items):
            page["next_page_token"] = str(start + int(max_results))
        return page

    def get_message(self, message_id: str):
        m = self.box.call("users.messages.get", {"id": message_id, "format": "full"})
        if "error" in m:
            return m
        h = {x["name"].lower(): x["value"] for x in m["payload"]["headers"]}
        return {"id": m["id"], "thread_id": m["threadId"], "labels": m["labelIds"], **h,
                "body": m["payload"]["body"]["data"]}  # the HTML part, as the API returns it

    def get_thread(self, thread_id: str):
        t = self.box.call("users.threads.get", {"id": thread_id})
        if "error" in t:
            return t
        return {"thread_id": thread_id, "messages": [self.get_message(m["id"]) for m in t["messages"]]}

    def list_labels(self):
        return self.box.call("users.labels.list", {})

    def create_label(self, name: str):
        return self.box.call("users.labels.create", {"name": name})

    def update_message_labels(self, message_ids: list[str], add_labels=None, remove_labels=None):
        return self.box.call("users.messages.batchModify", {"ids": message_ids, "addLabelIds": add_labels or [],
                                                            "removeLabelIds": remove_labels or []}) or {"ok": len(message_ids)}

    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None):
        if thread_id:
            self.drafts[thread_id] = {"to": [x.strip() for x in to.split(",") if x.strip()], "subject": subject,
                                      "body": body, "thread_id": thread_id}
        return self.box.call("users.drafts.create", {"to": to, "subject": subject, "body": body, "threadId": thread_id})

    def send_message(self, to: str, subject: str, body: str, cc: str | None = None, thread_id: str | None = None):
        return self.box.call("users.messages.send", {"to": to, "subject": subject, "body": body, "cc": cc,
                                                     "threadId": thread_id})

    def reply(self, message_id: str, body: str, reply_all: bool = False):
        m = self.box.msgs[message_id]
        to = m["from"] + ((", " + m["to"]) if reply_all else "")
        subj = m["subject"] if m["subject"].lower().startswith("re:") else "Re: " + m["subject"]
        return self.box.call("users.messages.send", {"to": to, "subject": subj, "body": body, "threadId": m["threadId"]})

    def forward(self, message_id: str, to: str, note: str = ""):
        m = self.box.msgs[message_id]
        body = (f"{note}\n\n---------- Forwarded message ----------\nFrom: {m['from']}\nSubject: {m['subject']}\n\n"
                + html_to_text(m["html"]))
        return self.box.call("users.messages.send", {"to": to, "subject": "Fwd: " + m["subject"], "body": body,
                                                     "threadId": m["threadId"]})

    def trash_message(self, message_id: str):
        return self.box.call("users.messages.trash", {"id": message_id})
