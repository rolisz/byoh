"""A real mailbox over IMAP + SMTP, behind the same Gmail-shaped API as the fake one.

The harness doesn't change: the connector, the policy and the checks call box.call(...) and read box.msgs, as
before. Only the world is different:

  read       the newest `limit` messages in INBOX are loaded once at start (BODY.PEEK: nothing is marked read)
  labels     IMAP keywords (custom flags). If the server doesn't allow keywords, labels stay in this session only
  archive    removing INBOX moves the message to the Archive folder; trash moves it to Trash
  UNREAD     removing it sets \\Seen; STARRED is \\Flagged
  drafts     appended to the Drafts folder (one per thread: a newer draft replaces the older one)
  send       SMTP, and a copy is appended to Sent

dry_run=True reads everything for real and writes nothing to the server: every write is logged to writes.jsonl.

Settings come from the environment (.env): IMAP_HOST, IMAP_USER, IMAP_PASSWORD, SMTP_HOST, SMTP_PORT, MAIL_NAME,
and optionally IMAP_PORT, SMTP_USER, SMTP_PASSWORD, ARCHIVE_FOLDER, TRASH_FOLDER, DRAFTS_FOLDER, SENT_FOLDER.
"""
import email
import imaplib
import os
import re
import smtplib
import time
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, formatdate, make_msgid, parsedate_to_datetime
from html import escape
from pathlib import Path

from fakemail import Mailbox

SYSTEM_LABELS = {"INBOX", "UNREAD", "STARRED", "SENT", "DRAFT", "TRASH", "SPAM", "IMPORTANT"}
MAX_BODY_BYTES = 2_000_000  # bigger messages (attachments) are loaded as headers only


def _q(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dec(value) -> str:
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except Exception:
        return str(value or "").strip()


def _keyword(label: str) -> str:
    """IMAP keywords are atoms: no spaces, parentheses, quotes or backslashes."""
    return re.sub(r"[^A-Za-z0-9_.\-$]", "_", label)


def _body_html(msg) -> str:
    html = text = None
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            payload = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/html" and html is None:
            html = payload
        elif ctype == "text/plain" and text is None:
            text = payload
    # the harness's cleaners expect HTML; plain text is wrapped so newlines survive html_to_text
    return html if html else "<pre>" + escape(text or "") + "</pre>"


def _uid_of(head: bytes) -> int | None:
    m = re.search(rb"UID (\d+)", head)
    return int(m.group(1)) if m else None


class ImapMailbox(Mailbox):
    def __init__(self, run_dir: Path, limit: int = 100, dry_run: bool = False, progress=print):
        env = os.environ
        self.dir, self.t0, self.dry_run, self.progress = run_dir, time.time(), dry_run, progress
        self.host, self.port = env["IMAP_HOST"], int(env.get("IMAP_PORT", 993))
        self.user, self.password = env["IMAP_USER"], env["IMAP_PASSWORD"]
        self.smtp = (env.get("SMTP_HOST", self.host.replace("imap", "smtp", 1)), int(env.get("SMTP_PORT", 587)),
                     env.get("SMTP_USER", self.user), env.get("SMTP_PASSWORD", self.password))
        self.owner = {"name": env.get("MAIL_NAME") or self.user.split("@")[0], "email": env.get("MAIL_ADDRESS", self.user)}
        self.today = datetime.now().strftime("%A %d %B %Y, %H:%M")
        self.config: dict = {}
        self.draft_uids: dict[str, int] = {}  # thread id -> uid of its draft in the Drafts folder
        self.imap = self._connect()
        self.folders = self._special_folders()
        self.msgs = self._load(limit)
        self._thread()
        self.labels = set(SYSTEM_LABELS) | {lab for m in self.msgs.values() for lab in m["labelIds"]}
        self._save()

    # ---------------------------------------------------------------- connection
    def _connect(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(self.host, self.port)
        imap.login(self.user, self.password)
        return imap

    def _imap(self, fn):
        """Run fn(imap); reconnect once if the server dropped us (long pauses on stage)."""
        try:
            return fn(self.imap)
        except (imaplib.IMAP4.abort, OSError):
            self.imap = self._connect()
            return fn(self.imap)

    def _special_folders(self) -> dict[str, str]:
        found = {}
        _, rows = self.imap.list()
        for row in rows or []:
            line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
            m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if not m:
                continue
            name = m.group("name").strip().strip('"')
            for use in ("Archive", "Trash", "Drafts", "Sent", "Junk"):
                if f"\\{use}" in m.group("flags"):
                    found.setdefault(use, name)
        env = os.environ
        return {use: env.get(f"{use.upper()}_FOLDER") or found.get(use) or use
                for use in ("Archive", "Trash", "Drafts", "Sent", "Junk")}

    # ---------------------------------------------------------------- loading
    def _load(self, limit: int) -> dict[str, dict]:
        imap = self.imap
        imap.select("INBOX")  # read-write, so PERMANENTFLAGS is real; BODY.PEEK below marks nothing as read
        perm = (imap.response("PERMANENTFLAGS")[1] or [b""])[0] or b""
        self.keywords_ok = b"\\*" in perm
        _, data = imap.uid("SEARCH", None, "ALL")
        uids = [int(x) for x in (data[0] or b"").split()][-limit:]
        self.progress(f"Loading {len(uids)} of the newest messages in INBOX from {self.host}…")
        meta: dict[int, dict] = {}
        for i in range(0, len(uids), 200):
            chunk = ",".join(map(str, uids[i:i + 200]))
            _, rows = imap.uid("FETCH", chunk, "(FLAGS RFC822.SIZE)")
            for row in rows:
                line = row if isinstance(row, bytes) else row[0]
                uid = _uid_of(line)
                if uid is None:
                    continue
                flags = re.search(rb"FLAGS \(([^)]*)\)", line)
                size = re.search(rb"RFC822\.SIZE (\d+)", line)
                meta[uid] = {"flags": (flags.group(1).decode() if flags else "").split(),
                             "size": int(size.group(1)) if size else 0}
        msgs = {}
        small = [u for u in uids if meta.get(u, {}).get("size", 0) <= MAX_BODY_BYTES]
        big = [u for u in uids if u not in small]
        for part, uids_part in (("BODY.PEEK[]", small), ("BODY.PEEK[HEADER]", big)):
            for i in range(0, len(uids_part), 25):
                _, rows = imap.uid("FETCH", ",".join(map(str, uids_part[i:i + 25])), f"(UID {part})")
                pending = None  # (raw, uid): some servers send UID after the literal, in the closing part
                for row in rows + [None]:
                    if pending and (pending[1] or row is None or isinstance(row, tuple)):
                        raw, uid = pending
                        if uid:
                            m = self._message(uid, email.message_from_bytes(raw), meta.get(uid, {}).get("flags", []))
                            if part.endswith("[HEADER]"):
                                m["html"] = f"<pre>[large message, {meta[uid]['size'] // 1000} kB: body not loaded]</pre>"
                            msgs[m["id"]] = m
                        pending = None
                    if isinstance(row, tuple):
                        pending = (row[1], _uid_of(row[0]))
                    elif isinstance(row, bytes) and pending and pending[1] is None:
                        pending = (pending[0], _uid_of(row))
                self.progress(f"  {len(msgs)}/{len(uids)}")
        return msgs

    def _message(self, uid: int, msg, flags: list[str]) -> dict:
        labels = ["INBOX"]
        if "\\Seen" not in flags:
            labels.append("UNREAD")
        if "\\Flagged" in flags:
            labels.append("STARRED")
        labels += [f for f in flags if not f.startswith("\\") and not f.startswith("$")]
        try:
            date = format_datetime(parsedate_to_datetime(msg["Date"]))
        except Exception:
            date = str(msg["Date"] or "")
        return {"id": f"u{uid:07d}", "threadId": None, "labelIds": labels,
                "from": _dec(msg["From"]), "to": _dec(msg["To"]), "cc": _dec(msg["Cc"]),
                "date": date, "subject": _dec(msg["Subject"]) or "(no subject)", "html": _body_html(msg),
                "_uid": uid, "_folder": "INBOX", "_msgid": (msg["Message-ID"] or "").strip(),
                "_refs": list(dict.fromkeys(re.findall(r"<[^>]+>", f"{msg['References'] or ''} {msg['In-Reply-To'] or ''}")))}

    def _thread(self):
        """Group messages into threads by Message-ID / In-Reply-To / References (union-find)."""
        parent: dict[str, str] = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for m in self.msgs.values():
            for ref in m["_refs"] + ([m["_msgid"]] if m["_msgid"] else []):
                parent[find(ref)] = find(m["id"])
        groups: dict[str, list[str]] = {}
        for m in self.msgs.values():
            groups.setdefault(find(m["id"]), []).append(m["id"])
        for ids in groups.values():
            tid = "t" + min(ids)[1:]
            for i in ids:
                self.msgs[i]["threadId"] = tid

    # ---------------------------------------------------------------- writes
    def _write(self, what: str, **details) -> bool:
        """Log every write; return True if it should really happen."""
        self._log("writes.jsonl", {"write": what, "dry_run": self.dry_run, **details})
        return not self.dry_run

    def _move(self, m: dict, folder: str):
        if not self._write("move", id=m["id"], subject=m["subject"][:80], to_folder=folder) or m["_uid"] is None:
            return

        def go(imap):
            imap.select(_q(m["_folder"]))
            if "MOVE" in imap.capabilities:
                imap.uid("MOVE", str(m["_uid"]), _q(folder))
            else:
                imap.uid("COPY", str(m["_uid"]), _q(folder))
                imap.uid("STORE", str(m["_uid"]), "+FLAGS.SILENT", "(\\Deleted)")
                imap.expunge()
            return imap.response("COPYUID")[1]

        copyuid = self._imap(go)
        new = re.search(rb"\d+ \S+ (\d+)", (copyuid or [b""])[-1] or b"")
        m["_uid"], m["_folder"] = (int(new.group(1)) if new else None), folder  # None: later changes stay local

    def _flags(self, m: dict, op: str, flags: list[str]):
        if not flags or not self._write("flags", id=m["id"], op=op, flags=flags) or m["_uid"] is None:
            return

        def go(imap):
            imap.select(_q(m["_folder"]))
            imap.uid("STORE", str(m["_uid"]), op + "FLAGS.SILENT", "(" + " ".join(flags) + ")")
        self._imap(go)

    def _messages_modify(self, id, addLabelIds=None, removeLabelIds=None, **_):
        m = self._get_msg(id)
        add = [x for x in addLabelIds or [] if x not in m["labelIds"]]
        remove = [x for x in removeLabelIds or [] if x in m["labelIds"]]
        for lab in add:
            if lab not in self.labels:
                raise ValueError(f"Label {lab} does not exist; create it first")
        def flag(label):  # None: this label has no IMAP form, it lives in this session only
            if label == "STARRED":
                return "\\Flagged"
            return None if label in SYSTEM_LABELS or not self.keywords_ok else _keyword(label)
        plus = [f for f in map(flag, add) if f]
        minus = [f for f in map(flag, remove) if f]
        if "UNREAD" in add:
            minus.append("\\Seen")
        if "UNREAD" in remove:
            plus.append("\\Seen")
        self._flags(m, "+", plus)
        self._flags(m, "-", minus)
        if "TRASH" in add:
            self._move(m, self.folders["Trash"])
        elif "SPAM" in add:
            self._move(m, self.folders["Junk"])
        elif "INBOX" in remove:
            self._move(m, self.folders["Archive"])
        elif "INBOX" in add and m["_folder"] != "INBOX":
            self._move(m, "INBOX")
        return super()._messages_modify(id, addLabelIds, removeLabelIds)

    def _messages_delete(self, id, **_):
        raise ValueError("Permanent delete is not available on a real mailbox. Use trash.")

    def _messages_batchDelete(self, ids, **_):
        raise ValueError("Permanent delete is not available on a real mailbox. Use trash.")

    def _email(self, to, subject, body, cc=None, threadId=None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self.owner["name"], self.owner["email"]))
        msg["To"], msg["Subject"], msg["Date"] = to, subject, formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.owner["email"].split("@")[-1])
        if cc:
            msg["Cc"] = cc
        thread = sorted((m for m in self.msgs.values() if m["threadId"] == threadId), key=lambda m: m["id"])
        if thread and thread[-1]["_msgid"]:
            msg["In-Reply-To"] = thread[-1]["_msgid"]
            msg["References"] = " ".join(thread[-1]["_refs"][-10:] + [thread[-1]["_msgid"]])
        msg.set_content(body)
        return msg

    def _append(self, folder: str, msg: EmailMessage, flags: str) -> int | None:
        def go(imap):
            imap.append(_q(folder), flags, imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            return imap.response("APPENDUID")[1]
        appended = self._imap(go)
        uid = re.search(rb"\d+ (\d+)", (appended or [b""])[-1] or b"")
        return int(uid.group(1)) if uid else None

    def delete_draft(self, thread_id: str):
        uid = self.draft_uids.pop(thread_id, None)
        if uid is None or not self._write("delete_draft", thread_id=thread_id, uid=uid):
            return

        def go(imap):
            imap.select(_q(self.folders["Drafts"]))
            imap.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
            if "UIDPLUS" in imap.capabilities:
                imap.uid("EXPUNGE", str(uid))
            else:
                imap.expunge()
        self._imap(go)

    def _drafts_create(self, **kw):
        kw = kw.get("message", kw)
        msg = self._email(kw["to"], kw["subject"], kw["body"], kw.get("cc"), kw.get("threadId"))
        if kw.get("threadId"):
            self.delete_draft(kw["threadId"])  # one draft per thread, like the connector's view of it
        if self._write("draft", to=kw["to"], subject=kw["subject"], thread_id=kw.get("threadId")):
            uid = self._append(self.folders["Drafts"], msg, "(\\Draft \\Seen)")
            if uid and kw.get("threadId"):
                self.draft_uids[kw["threadId"]] = uid
        return super()._drafts_create(**kw)

    def _messages_send(self, to, subject, body, cc=None, threadId=None, **_):
        msg = self._email(to, subject, body, cc, threadId)
        if self._write("send", to=to, cc=cc, subject=subject, thread_id=threadId):
            host, port, user, password = self.smtp
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
            self._append(self.folders["Sent"], msg, "(\\Seen)")
            if threadId:
                self.delete_draft(threadId)
        return super()._messages_send(to=to, subject=subject, body=body, cc=cc, threadId=threadId)
