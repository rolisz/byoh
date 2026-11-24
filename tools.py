"""Typed, domain-shaped tools (step 02+). They sit on top of the same fake mail API as step 00.

The point: no `send`, no `delete`, no filters. The agent can only do what this file lets it do,
and every action has arguments a policy can read (ids, labels, recipients) instead of a free-form API call.
"""
import re

from fakemail import Mailbox, html_to_text


def _addr(s: str) -> str:
    m = re.search(r"<([^>]+)>", s)
    return (m.group(1) if m else s).strip().lower()


def clean_body(html: str, limit: int = 2500) -> str:
    """Context work happens inside the tool: drop the quoted history, the signature, and the HTML."""
    html = re.split(r'(?i)<div class="gmail_quote"', html, maxsplit=1)[0]
    text = html_to_text(html)
    text = re.split(r"(?m)^--\s*$|^On .{5,80} wrote:$", text, maxsplit=1)[0].strip()
    text = re.sub(r"(?i)(unsubscribe|manage preferences|view in browser).*", "", text, flags=re.S).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n[... {len(text) - limit} more characters truncated]"
    return text


TOOLS = [
    {"name": "list_inbox",
     "description": "List messages currently in the inbox, newest first, as compact rows "
                    "(id, thread_id, from, subject, date, snippet). Use offset to page.",
     "input_schema": {"type": "object", "properties": {
         "limit": {"type": "integer", "default": 50}, "offset": {"type": "integer", "default": 0}}}},
    {"name": "read_email",
     "description": "Read one message as plain text, without HTML, quoted history or signature.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "label",
     "description": "Add a label to one or more messages. Labels are created on first use.",
     "input_schema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}}, "label": {"type": "string"}},
         "required": ["ids", "label"]}},
    {"name": "archive",
     "description": "Archive messages (remove them from the inbox; they stay searchable).",
     "input_schema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]}},
    {"name": "draft_reply",
     "description": "Save a draft reply in a thread for the user to review. Nothing is sent. "
                    "Recipients default to the other people on the thread.",
     "input_schema": {"type": "object", "properties": {
         "thread_id": {"type": "string"}, "body": {"type": "string"},
         "to": {"type": "array", "items": {"type": "string"}, "description": "Optional override"}},
         "required": ["thread_id", "body"]}},
    {"name": "ask_user",
     "description": "Ask Alex a question only Alex can answer (a decision, a fact you can't find). Keep it short.",
     "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
]


class MailTools:
    def __init__(self, box: Mailbox):
        self.box = box
        self.drafts: dict[str, dict] = {}  # latest draft per thread; a new draft replaces the old one

    def sender(self, id: str) -> str:
        return _addr(self.box.msgs[id]["from"])

    def thread_text(self, thread_id: str) -> str:
        msgs = sorted((m for m in self.box.msgs.values() if m["threadId"] == thread_id), key=lambda m: m["id"])
        return "\n\n".join(f"From: {m['from']}\nSubject: {m['subject']}\n{clean_body(m['html'], 4000)}" for m in msgs)

    def ask_user(self, question: str):
        return "Alex is not available. Decide yourself if it's safe, otherwise leave it for Alex."

    def thread_participants(self, thread_id: str) -> set[str]:
        msgs = [m for m in self.box.msgs.values() if m["threadId"] == thread_id]
        people = {_addr(m["from"]) for m in msgs} | {_addr(x) for m in msgs for x in m["to"].split(",")}
        return people - {self.box.owner["email"].lower()}

    def run(self, name: str, args: dict):
        return getattr(self, name)(**args)

    def list_inbox(self, limit: int = 50, offset: int = 0):
        ids = self.box.call("users.messages.list", {"q": "in:inbox", "maxResults": 500})["messages"]
        rows = []
        for ref in ids[offset:offset + limit]:
            m = self.box.msgs[ref["id"]]
            rows.append({"id": m["id"], "thread_id": m["threadId"], "from": m["from"], "subject": m["subject"],
                         "date": m["date"][5:22], "snippet": clean_body(m["html"], 140).replace("\n", " ")})
        return {"total": len(ids), "messages": rows}

    def read_email(self, id: str):
        m = self.box.call("users.messages.get", {"id": id})
        if "error" in m:
            return m
        h = {x["name"]: x["value"] for x in m["payload"]["headers"]}
        return {"id": id, "thread_id": m["threadId"], **h, "body": clean_body(m["payload"]["body"]["data"])}

    def label(self, ids: list[str], label: str):
        if label not in self.box.labels:
            self.box.call("users.labels.create", {"name": label})
        return self.box.call("users.messages.batchModify", {"ids": ids, "addLabelIds": [label]}) or {"ok": len(ids)}

    def archive(self, ids: list[str]):
        return self.box.call("users.messages.batchModify", {"ids": ids, "removeLabelIds": ["INBOX"]}) or {"ok": len(ids)}

    def draft_reply(self, thread_id: str, body: str, to: list[str] | None = None):
        msgs = [m for m in self.box.msgs.values() if m["threadId"] == thread_id]
        if not msgs:
            return {"error": f"Unknown thread_id {thread_id}. Use the thread_id from list_inbox or read_email."}
        to = to or sorted(self.thread_participants(thread_id))
        subject = msgs[0]["subject"] if msgs[0]["subject"].lower().startswith("re:") else "Re: " + msgs[0]["subject"]
        self.drafts[thread_id] = {"to": to, "subject": subject, "body": body, "thread_id": thread_id}
        return self.box.call("users.drafts.create", {"to": ", ".join(to), "subject": subject, "body": body,
                                                     "threadId": thread_id})
