"""The email agent with you in the loop, in the terminal: the same loop as agent.py, on a real mailbox or the fake one.

uv run tui.py                          # the fake inbox (Alex's), safe to play with
uv run tui.py --imap                   # your mailbox, from IMAP_* / SMTP_* in .env
uv run tui.py --imap --dry-run         # your mailbox, read for real, nothing written (writes go to writes.jsonl)

You type a request; the agent works; you see every tool call. While it works you can type more: it reads your
message after its current step. It asks you before bulk archives (step 2+) and before sending ANY email (always,
at every step: on a real mailbox this is the TUI's own rule). Replies it drafts show up on the right: open one
to edit, send or discard it. Esc stops the agent after its current step.
"""
import argparse
import json
import logging
import os
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from rich.markdown import Markdown
from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static, TextArea

from agent import MODEL, system_prompt
from checks import Human, Policy, _addr, clean, enrich, load_crm, verify
from connector import CONNECTOR_TOOLS as GMAIL_TOOLS, Connector
from fakemail import ROOT, Mailbox
from reviewer import gemini_needs_reply, gemini_reviewer
from tasks import TASKS
from tools import MailTools

load_dotenv()
log = logging.getLogger("byoh.tui")
PRICE = {"claude-haiku-5-5": (0.10, 0.50)}  # $ per 1M input / output, as in score.py; cache reads 0.1x, writes 1.25x
SENDING = {"send_message", "reply", "forward"}


class TuiPolicy(Policy):
    """The step-2 policy, plus the TUI's own rule: nothing is sent without the person saying so."""

    def __init__(self, box, crm, human, step: int):
        super().__init__(box, crm, human)
        self.step = step

    def check(self, name: str, args: dict):
        if name in SENDING:
            m = self.box.msgs.get(args.get("message_id", ""), {})
            to = args.get("to") or m.get("from", "?")
            subject = args.get("subject") or ("Re: " + m.get("subject", "") if name == "reply" else
                                              "Fwd: " + m.get("subject", "") if name == "forward" else "")
            body = args.get("body") or args.get("note") or ""
            answer = self.human.ask("send", f"Send this email to {to}?", f"Subject: {subject}\n\n{body}")
            if not answer.lower().startswith("y"):
                said = "" if answer.lower() in ("n", "no") else f" They said: {answer}"
                return self.deny(f"{self.who} did not approve sending this email.{said} "
                                 "If a reply is still needed, save it with create_draft instead.")
        return super().check(name, args) if self.step >= 2 else None


# ---------------------------------------------------------------- dialogs

class AskScreen(ModalScreen[str]):
    """The harness asks you something. Yes / No, or type your own answer."""
    BINDINGS = [Binding("escape", "dismiss('no')", "No")]

    def __init__(self, kind: str, question: str, details: str):
        super().__init__()
        self.kind, self.question, self.details = kind, question, details

    def compose(self) -> ComposeResult:
        yes, no = ("Send", "Don't send") if self.kind == "send" else ("Yes", "No")
        with Vertical(classes="dialog"):
            yield Label(f"The harness asks · {self.kind}", classes="kind")
            yield Static(escape(self.question), classes="question")
            with VerticalScroll(classes="details"):
                yield Static(escape(self.details))
            yield Input(placeholder="…or type an answer for the agent, then Enter", id="answer")
            with Horizontal(classes="buttons"):
                yield Button(yes, variant="success", id="yes")
                yield Button(no, variant="error", id="no")

    def on_mount(self):
        self.query_one("#yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id)

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or "no")


class DraftScreen(ModalScreen[tuple[str, str] | None]):
    """One draft: edit it, send it, or throw it away. Sending from here is you, not the agent."""
    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, draft: dict, flags: list[str], note: str = ""):
        super().__init__()
        self.draft, self.flags, self.note = draft, flags, note

    def compose(self) -> ComposeResult:
        d = self.draft
        with Vertical(classes="dialog"):
            yield Label(escape(f"To: {', '.join(d['to'])}"), classes="kind")
            yield Static(escape(d["subject"]), classes="question")
            yield TextArea(d["body"], id="body")
            if self.flags:
                yield Static("\n".join(f"[b red]⚑[/] {escape(f)}" for f in self.flags), classes="flags")
            elif d.get("reviewed"):
                yield Static("[green]Reviewer: no flags[/]", classes="flags")
            if self.note:
                yield Static(f"[dim]Reviewer: {escape(self.note)}[/]", classes="flags")
            with Horizontal(classes="buttons"):
                yield Button("Send", variant="success", id="send", disabled=bool(d.get("sent")))
                yield Button("Save", variant="primary", id="save")
                yield Button("Discard", variant="error", id="discard")
                yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "close":
            self.dismiss(None)
        else:
            self.dismiss((event.button.id, self.query_one("#body", TextArea).text))


# ---------------------------------------------------------------- the app

class Byoh(App):
    TITLE = "BYOH"
    CSS = """
    #main { width: 2fr; }
    #side { width: 1fr; border-left: solid $panel-lighten-2; }
    #log { height: 1fr; padding: 0 1; }
    #prompt { margin: 0 1 1 1; }
    .side-title { padding: 1 1 0 1; text-style: bold; }
    #drafts { height: 1fr; }
    #stats { height: auto; padding: 1; color: $text-muted; }
    AskScreen, DraftScreen { align: center middle; }
    .dialog { width: 100; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 1 2; }
    .kind { color: $text-muted; }
    .question { text-style: bold; margin: 1 0; }
    .details { height: auto; max-height: 24; border: round $panel-lighten-2; padding: 0 1; }
    .flags { margin-top: 1; }
    #body { height: 18; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    """
    BINDINGS = [Binding("escape", "stop", "Stop the agent"), Binding("ctrl+o", "focus_drafts", "Drafts"),
                Binding("ctrl+t", "focus_prompt", "Type")]

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.messages: list[dict] = []
        self.steer: list[str] = []      # what you typed while the agent was working
        self.running = self.stop = False
        self.box = None
        self.cost = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="main"):
                yield RichLog(id="log", wrap=True, markup=True)
                yield Input(value=TASKS["morning"], id="prompt", disabled=True)
            with Vertical(id="side"):
                yield Label("Replies ready", classes="side-title")
                yield ListView(id="drafts")
                yield Static("", id="stats")
        yield Footer()

    def on_mount(self):
        self.setup()

    # ---- output, always on the UI thread
    def say(self, text, style: str = ""):
        self.query_one("#log", RichLog).write(f"[{style}]{text}[/]" if style else text)

    def say_md(self, text: str):
        self.query_one("#log", RichLog).write(Markdown(text))

    def ui(self, fn, *a):
        """From the agent's thread: run fn on the UI thread."""
        self.call_from_thread(fn, *a)

    # ---- setup: load the mailbox, build the harness
    @work(thread=True)
    def setup(self):
        a = self.args
        name = a.name or datetime.now().strftime("tui-%Y%m%d-%H%M%S")
        try:
            if a.imap:
                from imapmail import ImapMailbox
                missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD") if not os.environ.get(k)]
                if missing:
                    self.ui(self.say, f"Set {', '.join(missing)} in .env first (see .env.example).", "bold red")
                    return
                run_dir = ROOT / "runs" / name
                run_dir.mkdir(parents=True, exist_ok=True)
                box = ImapMailbox(run_dir, limit=a.limit, dry_run=a.dry_run, progress=lambda s: self.ui(self.say, s, "dim"))
                if not box.keywords_ok:
                    self.ui(self.say, "This server doesn't allow IMAP keywords: labels stay in this session only.", "yellow")
            else:
                box = Mailbox.fresh(name)
        except Exception as e:
            log.exception("could not open the mailbox")
            self.ui(self.say, f"Could not open the mailbox: {escape(repr(e))}", "bold red")
            return
        logging.basicConfig(filename=box.dir / "tui.log", level=logging.INFO)
        crm_path = Path(a.crm) if a.crm else (ROOT / "my_crm.json" if a.imap else ROOT / "crm.json")
        crm = load_crm(crm_path) if crm_path.exists() else {}
        self.box, self.crm, step = box, crm, a.step
        self.gmail = Connector(box)
        self.mail = MailTools(box)
        self.mail.drafts = self.gmail.drafts
        self.human = Human(box.dir, asker=self.ask_human)
        self.policy = TuiPolicy(box, crm, self.human, step)
        self.report: dict = {}
        owner = box.owner["name"]
        self.check = verify(self.mail, set(), gemini_reviewer(owner), self.report, gemini_needs_reply(owner)) \
            if step >= 3 else None
        self.system = system_prompt(box, step)
        self.extra = {"cache_control": {"type": "ephemeral"}} if step >= 1 else {}
        self.client = anthropic.Anthropic()
        self.trace = open(box.dir / "trace.jsonl", "a")
        inbox = sum(1 for m in box.msgs.values() if "INBOX" in m["labelIds"])
        mode = "DRY RUN · nothing is written" if getattr(box, "dry_run", False) else \
            ("LIVE · writes go to your mailbox" if a.imap else "fake inbox")
        self.ui(setattr, self, "sub_title", f"{box.owner['email']} · step {step} · {mode}")
        self.ui(self.say, f"[b]{inbox}[/b] emails in the inbox · CRM: {len(crm)} contacts"
                          f"{'' if crm else ' (none: create my_crm.json, same shape as crm.json)'} · run: runs/{name}")
        self.ui(self.say, "Edit the request below, or press Enter to send it.", "dim")
        self.ui(self.ready)

    def ready(self):
        box = self.query_one("#prompt", Input)
        box.disabled = False
        box.focus()

    # ---- the human, as the harness sees it
    def ask_human(self, kind: str, question: str, details: str) -> str:
        """Called from the agent's thread; blocks until you answer in the dialog."""
        answer: Future = Future()
        self.ui(self.push_screen, AskScreen(kind, question, details), answer.set_result)
        result = answer.result()
        self.ui(self.say, f"  you: {escape(result)}", "bold magenta")
        return result

    # ---- input
    @on(Input.Submitted, "#prompt")
    def submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if not text or self.box is None:
            return
        if self.running:
            self.steer.append(text)
            self.say(f"[b]you[/b] (the agent reads this after its current step): {escape(text)}", "magenta")
        else:
            self.say(f"\n[b]you:[/b] {escape(text)}", "magenta")
            self.run_agent(text)

    def action_stop(self):
        if self.running:
            self.stop = True
            self.say("Stopping after the current step…", "yellow")

    def action_focus_drafts(self):
        self.query_one("#drafts", ListView).focus()

    def action_focus_prompt(self):
        self.query_one("#prompt", Input).focus()

    # ---- the loop: agent.py's loop, with print() replaced by the screen
    @work(thread=True, exclusive=True)
    def run_agent(self, text: str):
        self.running, self.stop = True, False
        if self.steer:                                               # e.g. "(I sent the reply in thread …)"
            text, self.steer = "\n".join(self.steer + [text]), []
        if self.messages and self.messages[-1]["role"] == "user":   # stopped mid-run: the tool results are last
            last = self.messages[-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].append({"type": "text", "text": text})
        else:
            self.messages.append({"role": "user", "content": text})
        rounds, t0 = 0, time.time()
        try:
            while not self.stop:
                t = time.time()
                response = self.client.messages.create(model=MODEL, max_tokens=16000, system=self.system,
                                                       tools=GMAIL_TOOLS, messages=self.messages, **self.extra)
                self.account(response, time.time() - t, t - t0)
                self.messages.append({"role": "assistant", "content": response.content})
                for b in response.content:
                    if b.type == "text" and b.text.strip():
                        self.ui(self.say_md, b.text)

                if response.stop_reason != "tool_use":
                    if self.check and rounds < 2:                            # step 3: check before you see it
                        self.ui(self.say, "  checking the work…", "dim")
                        if problems := self.check():
                            rounds += 1
                            self.ui(self.say, f"  verification sent it back:\n{escape(problems)}", "yellow")
                            self.messages.append({"role": "user", "content": f"Before you finish, fix these:\n{problems}"})
                            continue
                    break

                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        t = time.time()
                        out = self.policy.check(block.name, block.input)    # allow, deny or ask you
                        if out is None:
                            out = self.gmail.run(block.name, block.input)
                            if self.args.step >= 1:
                                out = enrich(clean(out), self.crm)          # step 1: context
                        result = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                        self.trace.write(json.dumps({"t": round(t - t0, 2), "kind": "tool.call", "name": block.name,
                                                     "input": block.input, "output": result[:300]},
                                                    ensure_ascii=False) + "\n")
                        self.ui(self.show_tool, block.name, block.input, out)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": result,
                                        **({"is_error": True} if isinstance(out, dict) and "error" in out else {})})
                if self.steer:                                               # what you typed meanwhile
                    said, self.steer = self.steer, []
                    results.append({"type": "text", "text": "\n".join(said)})
                self.messages.append({"role": "user", "content": results})
                self.ui(self.refresh_drafts)
            if self.stop:
                self.ui(self.say, "Stopped. Type to continue.", "yellow")
        except Exception as e:
            log.exception("agent run failed")
            last = self.messages[-1] if self.messages else {}
            if last.get("role") == "assistant" and any(getattr(b, "type", "") == "tool_use" for b in last["content"]):
                self.messages.pop()                                      # its tool calls never got results
            self.ui(self.say, f"The run failed: {escape(repr(e))}", "bold red")
        finally:
            self.trace.flush()
            self.running = False
            self.ui(self.refresh_drafts)

    def account(self, response, seconds: float, at: float):
        self.box.log_model_call(response, seconds)
        u = response.usage
        cache_r, cache_w = u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0
        pin, pout = PRICE.get(MODEL, (0, 0))
        self.cost += (u.input_tokens * pin + cache_r * pin * 0.1 + cache_w * pin * 1.25 + u.output_tokens * pout) / 1e6
        self.trace.write(json.dumps({"t": round(at, 2), "kind": "model.call", "seconds": round(seconds, 2),
                                     "stop": response.stop_reason, "in": u.input_tokens, "out": u.output_tokens,
                                     "cache_read": cache_r, "cache_write": cache_w}) + "\n")
        ctx = u.input_tokens + cache_r + cache_w
        self.ui(self.set_stats,
                f"context {ctx / 1000:.1f}k tokens · {cache_r / max(ctx, 1):.0%} cached\n"
                f"cost so far ${self.cost:.3f} (Haiku; Gemini not included)")

    def set_stats(self, text: str):
        self.query_one("#stats", Static).update(text)

    def show_tool(self, name: str, args: dict, out):
        msgs = self.box.msgs
        subj = lambda i: msgs.get(i, {}).get("subject", i)[:60]
        if isinstance(out, dict) and "error" in out:
            err = out["error"] if isinstance(out["error"], str) else out["error"].get("message", out["error"])
            colour = "bold red" if str(err).startswith("Denied by policy") else "red"
            self.say(f"  ✗ {name}: {escape(str(err))}", colour)
            return
        if name == "search_threads":
            line = f"search {args.get('query', '')!r} → {len(out.get('threads', []))} of {out.get('total_threads')} threads"
        elif name == "get_message":
            line = f"read: {subj(args.get('message_id'))}"
        elif name == "get_thread":
            ms = out.get("messages", [])
            line = f"read thread: {(ms[0].get('subject', '') if ms else '')[:60]} ({len(ms)} messages)"
        elif name == "update_message_labels":
            ids = args.get("message_ids", [])
            add, rem = args.get("add_labels") or [], args.get("remove_labels") or []
            what = [f"+{x}" for x in add] + ["archive" if x == "INBOX" else f"−{x}" for x in rem]
            line = f"{' '.join(what)} · {len(ids)} email{'s' * (len(ids) != 1)}: " + "; ".join(subj(i)[:30] for i in ids[:3]) \
                + (" …" if len(ids) > 3 else "")
        elif name == "create_draft":
            line = f"draft to {args.get('to')}: {args.get('subject', '')[:60]}"
        elif name in SENDING:
            line = f"SENT ({name}) to {args.get('to') or msgs.get(args.get('message_id'), {}).get('from', '?')}"
        elif name == "trash_message":
            line = f"trash: {subj(args.get('message_id'))}"
        else:
            line = f"{name} {json.dumps(args, ensure_ascii=False)[:80]}"
        self.say(f"  → {escape(line)}", "bold green" if name in SENDING else "dim")

    # ---- the drafts, on the right
    def flags_for(self, thread: str) -> list[str]:
        r = self.report.get(thread)
        if not r:
            return []
        rv = r.get("review") or {}
        out = [f"Commitment: {c}" for c in rv.get("commitments", [])]
        out += [f"Possible leak: {x}" for x in r.get("leaks", []) + rv.get("leaks", [])]
        if rv and not rv.get("recipient_ok", True):
            out.append("Reviewer: the recipients look wrong")
        return out

    def note_for(self, thread: str) -> str:
        return ((self.report.get(thread) or {}).get("review") or {}).get("note", "")

    def refresh_drafts(self):
        lv = self.query_one("#drafts", ListView)
        lv.clear()
        for thread, d in self.gmail.drafts.items():
            d["reviewed"] = thread in self.report
            n = len(self.flags_for(thread))
            mark = "[green]✓ sent[/] " if d.get("sent") else (f"[b red]⚑{n}[/] " if n else "")
            who = ", ".join(_addr(x) for x in d["to"])[:34]
            lv.append(ListItem(Label(f"{mark}{escape(who)}\n[dim]{escape(d['subject'][:40])}[/]"), name=thread))

    @on(ListView.Selected, "#drafts")
    def open_draft(self, event: ListView.Selected):
        thread = event.item.name
        draft = self.gmail.drafts.get(thread)
        if draft:
            self.push_screen(DraftScreen(draft, self.flags_for(thread), self.note_for(thread)),
                             lambda result: result and self.draft_action(thread, *result))

    @work(thread=True)
    def draft_action(self, thread: str, action: str, body: str):
        d = self.gmail.drafts.get(thread)
        if not d:
            return
        try:
            if action == "send":
                self.box.call("users.messages.send", {"to": ", ".join(d["to"]), "subject": d["subject"],
                                                      "body": body, "threadId": thread})
                d.update(body=body, sent=True)
                self.ui(self.say, f"You sent the reply to {escape(', '.join(d['to']))}.", "green")
                self.steer.append(f"(I sent the reply in thread {thread}.)")
            elif action == "save":
                self.box.call("users.drafts.create", {"to": ", ".join(d["to"]), "subject": d["subject"],
                                                      "body": body, "threadId": thread})
                d["body"] = body
                self.ui(self.say, f"Draft to {escape(', '.join(d['to']))} saved.", "dim")
            elif action == "discard":
                self.gmail.drafts.pop(thread, None)
                self.report.pop(thread, None)
                if hasattr(self.box, "delete_draft"):
                    self.box.delete_draft(thread)
                self.ui(self.say, "Draft discarded.", "dim")
                self.steer.append(f"(I discarded the draft in thread {thread}.)")
        except Exception as e:
            log.exception("draft action %s failed", action)
            self.ui(self.say, f"{action} failed: {escape(repr(e))}", "bold red")
        self.ui(self.refresh_drafts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--imap", action="store_true", help="use the real mailbox from .env instead of the fake inbox")
    ap.add_argument("--dry-run", action="store_true", help="with --imap: read for real, write nothing")
    ap.add_argument("--step", type=int, default=3, help="harness step 0-3 (default 3)")
    ap.add_argument("--limit", type=int, default=100, help="with --imap: how many of the newest inbox emails to load")
    ap.add_argument("--crm", help="CRM file (default: crm.json for the fake inbox, my_crm.json with --imap)")
    ap.add_argument("--name", help="run folder name under runs/")
    Byoh(ap.parse_args()).run()


if __name__ == "__main__":
    main()
