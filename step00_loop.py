"""Step 00: the bare loop, with the tools of a typical Gmail connector. No harness. This is the slide.

uv run step00_loop.py [run-name] ["task"]
"""
import json
import sys
import time

import anthropic
from dotenv import load_dotenv

from connector import CONNECTOR_TOOLS as GMAIL_TOOLS, Connector
from fakemail import Mailbox
from tasks import TASKS

load_dotenv()

client = anthropic.Anthropic()
box = Mailbox.fresh(sys.argv[1] if len(sys.argv) > 1 else None)
gmail = Connector(box)  # the tools of a typical Gmail connector
system = f"You are {box.owner['name']}'s email assistant ({box.owner['email']}). Now: {box.today}."
task = sys.argv[2] if len(sys.argv) > 2 else "triage"
messages = [{"role": "user", "content": TASKS.get(task, task)}]

for turn in range(200):  # safety cap for the experiments; on the slide this is `while True:`
    t = time.time()
    response = client.messages.create(model="claude-haiku-5-5", max_tokens=16000,
                                      system=system, tools=GMAIL_TOOLS, messages=messages)
    box.log_model_call(response, time.time() - t)  # measurement only, not part of the harness
    messages.append({"role": "assistant", "content": response.content})
    if response.stop_reason != "tool_use":
        break
    results = []
    for block in response.content:
        if block.type == "tool_use":
            output = gmail.run(block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)})
    messages.append({"role": "user", "content": results})

print("\n".join(b.text for b in response.content if b.type == "text"))
(box.dir / "transcript.json").write_text(json.dumps(
    [m if isinstance(m["content"], str) else {"role": m["role"], "content": [
        c if isinstance(c, dict) else c.model_dump(exclude_none=True) for c in m["content"]]} for m in messages],
    indent=1, ensure_ascii=False))
