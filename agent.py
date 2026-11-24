"""The email agent: one loop around a Gmail connector's tools. Each step of the talk adds a few lines (the talk's git tags, as a flag).

uv run agent.py --step 3 --task morning --name final-01 [--human interactive]

  step 0  the bare loop, with a Gmail connector's 11 tools                         (same as step00_loop.py)
  step 1  context:       clean the email bodies, attach what the CRM knows about each sender, Alex's rules
                         in the system prompt, prompt caching (one argument)
  step 2  control:       a policy checked before every tool call: allow, deny with a reason, or ask Alex
  step 3  verification:  checks when the model says it's done, a second-lab reviewer, one review page

The tools never change: the same 11, send included.
"""
import argparse
import json
import time

import anthropic
from dotenv import load_dotenv

from checks import Human, Policy, clean, enrich, load_crm, verify
from connector import CONNECTOR_TOOLS as GMAIL_TOOLS, Connector
from fakemail import Mailbox
from reviewer import gemini_needs_reply, gemini_reviewer
from review_page import write_review_page
from tasks import TASKS
from tools import MailTools

load_dotenv()
MODEL = "claude-haiku-5-5"


def system_prompt(box: Mailbox, step: int) -> str:
    base = f"You are {box.owner['name']}'s email assistant ({box.owner['email']}). Now: {box.today}."
    if step == 0:
        return base
    who = box.owner["name"].split()[0]
    return base + (                                                          # step 1: the owner's rules
        f"\nEvery sender comes with what {who}'s CRM knows about them. Never archive email from high-priority "
        f"contacts: label it so {who} sees it.\n"
        f"Never make commitments for {who} (dates, prices, accepting work, personal plans) in a reply: "
        f"leave a [placeholder] for {who} to fill in.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--task", default="morning")
    ap.add_argument("--name")
    ap.add_argument("--human", default="auto", choices=["auto", "interactive"])
    a = ap.parse_args()
    step = a.step

    client = anthropic.Anthropic()
    box = Mailbox.fresh(a.name)
    gmail, human, crm = Connector(box), Human(box.dir, a.human), load_crm()
    mail = MailTools(box)       # helpers the checks use (threads, senders); its drafts are the connector's
    mail.drafts = gmail.drafts
    system = system_prompt(box, step)

    extra = {"cache_control": {"type": "ephemeral"}} if step >= 1 else {}   # step 1: context
    policy = Policy(box, crm, human) if step >= 2 else None                 # step 2: control
    report: dict = {}
    check = verify(mail, set(), gemini_reviewer(box.owner['name']), report, gemini_needs_reply(box.owner['name'])) if step >= 3 else None  # step 3

    trace, t0 = open(box.dir / "trace.jsonl", "a"), time.time()  # measurement only: the audit log
    messages = [{"role": "user", "content": TASKS.get(a.task, a.task)}]
    verify_rounds = 0

    for turn in range(200):  # safety cap for the experiments; on the slides this is `while True:`
        t = time.time()
        response = client.messages.create(model=MODEL, max_tokens=16000, system=system,
                                          tools=GMAIL_TOOLS, messages=messages, **extra)
        box.log_model_call(response, time.time() - t)                     # measurement only
        u = response.usage
        trace.write(json.dumps({"t": round(t - t0, 2), "kind": "model.call", "seconds": round(time.time() - t, 2),
                                "stop": response.stop_reason, "in": u.input_tokens, "out": u.output_tokens,
                                "cache_read": u.cache_read_input_tokens or 0,
                                "cache_write": u.cache_creation_input_tokens or 0}) + "\n")
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            if check and verify_rounds < 2 and (problems := check()):     # step 3: check before you see it
                verify_rounds += 1
                messages.append({"role": "user", "content": f"Before you finish, fix these:\n{problems}"})
                continue
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                t = time.time()
                out = policy.check(block.name, block.input) if policy else None   # step 2: allow, deny or ask
                if out is None:
                    out = gmail.run(block.name, block.input)
                    if step >= 1:
                        out = enrich(clean(out), crm)                              # step 1: context
                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                trace.write(json.dumps({"t": round(t - t0, 2), "kind": "tool.call", "seconds": round(time.time() - t, 2),
                                        "name": block.name, "input": block.input, "output": text[:300]},
                                       ensure_ascii=False) + "\n")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                **({"is_error": True} if isinstance(out, dict) and "error" in out else {})})
        messages.append({"role": "user", "content": results})
    trace.close()

    final = messages[-1]["content"]
    summary = "\n".join(b.text for b in final if getattr(b, "type", "") == "text") if isinstance(final, list) else final
    print(summary)
    if step >= 3:
        if not report:  # verify never ran: review what's there
            verify(mail, set(), gemini_reviewer(box.owner['name']), report)()
        print(f"\nreview page: {write_review_page(box, mail, report, summary)}")
    (box.dir / "transcript.json").write_text(json.dumps(
        [m if isinstance(m["content"], str) else {"role": m["role"], "content": [
            c if isinstance(c, dict) else c.model_dump(exclude_none=True) for c in m["content"]]} for m in messages],
        indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
