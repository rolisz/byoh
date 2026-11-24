"""What the harness adds around the Gmail tools, one group per box. Plain functions; the loop decides where they run.

  context:       clean(out), enrich(out, crm)        after a tool runs: readable bodies, who each sender is
  control:       Policy.check(name, args)            before a tool runs: None = go ahead, else the answer
  verification:  verify(...)() -> problems or None   when the model says it's done
  human:         the person the harness asks; `auto` mode stands in for a person during experiments
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tools import MailTools, _addr, clean_body


# ---------------------------------------------------------------- the human

class Human:
    """Who the harness asks. `interactive` prompts in the terminal; `auto` is a scripted stand-in for experiments;
    `asker` hands the question to a UI (the TUI) and blocks until the person answers."""

    def __init__(self, run_dir: Path, mode: str = "auto", asker=None):
        self.mode, self.log, self.asker = mode, run_dir / "asks.jsonl", asker

    def ask(self, kind: str, question: str, details: str = "") -> str:
        if self.asker:
            answer = self.asker(kind, question, details)
        elif self.mode == "interactive":
            print(f"\n=== {kind} ===\n{question}\n{details}")
            answer = input("> ").strip() or "no"
        else:
            # The stand-in approves bulk archives and one budget extension, and answers nothing else.
            answer = {"bulk_archive": "yes", "budget": "yes"}.get(
                kind, "I'm not available right now. Leave it for me, with a placeholder if needed.")
        with open(self.log, "a") as f:
            f.write(json.dumps({"kind": kind, "question": question, "details": details[:2000], "answer": answer},
                               ensure_ascii=False) + "\n")
        return answer


# ---------------------------------------------------------------- context

def load_crm(path: Path = Path(__file__).parent / "crm.json") -> dict[str, dict]:
    return {c["email"].lower(): c for c in json.loads(path.read_text())["contacts"]}


def crm_line(sender: str, crm: dict[str, dict]) -> str:
    c = crm.get(_addr(sender))
    if not c:
        return "not in CRM"
    who = ", ".join(x for x in (c["role"], c["company"]) if x)
    return f"{who} · {c['relationship']} · priority: {c['priority']}"


def enrich(out, crm: dict[str, dict]):
    """Next to every sender in a tool result, what the CRM knows about them. Deterministic: no tool to forget to call."""
    if isinstance(out, dict):
        out = {k: enrich(v, crm) for k, v in out.items()}
        if isinstance(out.get("from"), str):
            out["crm"] = crm_line(out["from"], crm)
        return out
    if isinstance(out, list):
        return [enrich(x, crm) for x in out]
    return out


def clean(out):
    """Email bodies as the model should read them: no HTML, quoted history, signature or unsubscribe footer."""
    if isinstance(out, dict):
        return {k: (clean_body(v) if k == "body" and isinstance(v, str) else clean(v)) for k, v in out.items()}
    if isinstance(out, list):
        return [clean(x) for x in out]
    return out


# ---------------------------------------------------------------- control

class Policy:
    """Checked before every tool call: allow (None), deny with a reason, or ask the human. Reads the Gmail tools' own arguments."""

    def __init__(self, box, crm: dict[str, dict], human: Human, bulk: int = 15):
        self.box, self.crm, self.human, self.bulk = box, crm, human, bulk
        self.who = box.owner["name"].split()[0]

    @staticmethod
    def deny(reason: str) -> dict:
        return {"error": f"Denied by policy: {reason}"}

    def check(self, name: str, args: dict):
        if name == "update_message_labels" and "INBOX" in (args.get("remove_labels") or []):  # i.e. archive
            ids = [i for i in args.get("message_ids", []) if i in self.box.msgs]
            key = [i for i in ids if self.crm.get(_addr(self.box.msgs[i]["from"]), {}).get("priority") == "high"]
            if key:
                return self.deny(f"{key} are from high-priority contacts in {self.who}'s CRM and stay in the inbox. "
                                 f"Label them instead, and archive the others without them.")
            if len(ids) > self.bulk:
                senders = sorted({_addr(self.box.msgs[i]["from"]) for i in ids})
                preview = "\n".join(f"- {self.box.msgs[i]['from']}: {self.box.msgs[i]['subject'][:70]}" for i in ids)
                answer = self.human.ask("bulk_archive", f"Archive {len(ids)} emails from {len(senders)} senders?", preview)
                if not answer.lower().startswith("y"):
                    said = "" if answer.lower() in ("n", "no") else f" They said: {answer}"
                    return self.deny(f"{self.who} declined archiving these {len(ids)} emails.{said} "
                                     "Leave them in the inbox unless that says otherwise.")
        return None


# ---------------------------------------------------------------- verification

MONEY_OR_NUMBER = re.compile(r"\b\d{1,3}(?:[.,]\d{3})+\b|\b(?!(?:19|20)\d\d\b)\d{4,}\b|(?:EUR|€|\$)\s?\d[\d.,]*")


def leak_check(tools: MailTools, draft: dict) -> list[str]:
    """Numbers and amounts in a draft that appear in *another* thread but not in this one."""
    own = tools.thread_text(draft["thread_id"])
    others = " ".join(tools.thread_text(t) for t in {m["threadId"] for m in tools.box.msgs.values()}
                      if t != draft["thread_id"])
    found = []
    for tok in set(MONEY_OR_NUMBER.findall(draft["body"])):
        digits = re.sub(r"\D", "", tok)
        if digits and digits not in re.sub(r"\D", " ", own).split() and tok not in own \
                and (tok in others or digits in re.sub(r"[.,]", "", others)):
            found.append(tok)
    return found


def verify(tools: MailTools, vips: set[str], reviewer=None, report: dict | None = None, needs_reply=None):
    """Runs when the model says it's done. Returns problems for the model to fix, or None.
    Commitments are not problems for the model: they're decisions for Alex, collected in `report`."""
    report = report if report is not None else {}

    def check() -> str | None:
        problems, who = [], tools.box.owner["name"].split()[0]
        user_labels = {"INBOX", "UNREAD"}
        untouched = [m["id"] for m in tools.box.msgs.values()
                     if "INBOX" in m["labelIds"] and not (set(m["labelIds"]) - user_labels)]
        if untouched:
            problems.append(f"{len(untouched)} inbox emails were neither labelled nor archived: {untouched}")
        # The second-model calls are slow (5-20 s each), so they all run at once; local checks stay on this thread.
        drafts = list(tools.drafts.items())
        # The checker reads the whole email, not the snippet the agent judged by: a check that sees only
        # what the agent saw makes the same mistakes (it filed the CEO's question as a newsletter too).
        rows = [{**r, "body": clean_body(tools.box.msgs[r["id"]]["html"], 2000)}
                for r in tools.list_inbox(limit=500)["messages"] if r["thread_id"] not in tools.drafts] \
            if needs_reply else []
        with ThreadPoolExecutor(max_workers=5) as pool:  # more than ~5 at once drew 503s
            pending_nr = pool.submit(needs_reply, rows) if needs_reply else None
            pending = {t: pool.submit(reviewer, d, tools.thread_text(t)) for t, d in drafts} if reviewer else {}
            missing = [i for i in pending_nr.result() if i in tools.box.msgs] if pending_nr else []
            reviews = {t: f.result() for t, f in pending.items()}
        if missing:  # a second model's opinion: what still expects an answer but has no draft?
            problems.append(f"These emails expect a reply from {who} but have no draft: {missing}. Draft a reply "
                            f"for each, with [placeholders] for anything only {who} can decide.")
        for t, d in drafts:
            leaks = leak_check(tools, d)
            review = reviews.get(t, {})
            report[t] = {"draft": d, "leaks": leaks, "review": review}
            leaks += review.get("leaks", [])
            if leaks:
                problems.append(f"Draft in thread {t} to {d['to']} contains information from outside its thread: "
                                f"{leaks}. Rewrite it without that.")
        return "\n".join(f"- {p}" for p in problems) or None
    return check
