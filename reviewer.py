"""A second model, from another lab, reviews every draft before Alex sees it. A council of two, as a safety check."""
import json
import logging
import os

log = logging.getLogger("byoh.reviewer")

def _client():
    """Gemini returns 503 'high demand' under bursts (we saw it with 10 parallel reviews): retry with backoff."""
    from google import genai
    from google.genai import types
    return genai.Client(http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(
        attempts=4, initial_delay=1.0, max_delay=8.0, http_status_codes=[429, 500, 503])))


REVIEW_MODEL = "gemini-4-hydrogen"  # Google's new lightweight model; listed by the API on 2026-11-23 (was gemini-3.8-flash)

SCHEMA = {
    "type": "object",
    "properties": {
        "commitments": {"type": "array", "items": {"type": "string"},
                        "description": "Every promise the draft makes on Alex's behalf: dates, deadlines, accepting "
                                       "work, prices, personal plans. Quote the words."},
        "leaks": {"type": "array", "items": {"type": "string"},
                  "description": "Facts in the draft that do not come from this thread and could be confidential."},
        "recipient_ok": {"type": "boolean"},
        "note": {"type": "string", "description": "One sentence for Alex, or empty."},
    },
    "required": ["commitments", "leaks", "recipient_ok", "note"],
}

PROMPT = """You review an email draft that an AI assistant wrote for Alex Moldovan. Alex will read your review before
deciding whether to send it. Be specific and short.

THREAD (what the assistant was replying to):
{thread}

DRAFT to {to}:
{body}

List every commitment the draft makes for Alex, any information that does not come from the thread above
(possible leak), and whether the recipients make sense for this thread."""


NEEDS_REPLY_SCHEMA = {
    "type": "object",
    "properties": {"needs_reply": {"type": "array", "items": {"type": "string"},
                                   "description": "ids of the emails a person would expect Alex to answer"}},
    "required": ["needs_reply"],
}


def _named(text: str, owner: str) -> str:
    return text.replace("Alex Moldovan", owner).replace("Alex", owner.split()[0])


def gemini_needs_reply(owner: str = "Alex Moldovan"):
    """Cheap second opinion for verification: which of these emails expect an answer from Alex?"""
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return None
    from google.genai import types
    client = _client()

    def check(rows: list[dict]) -> list[str]:
        listing = "\n\n".join(f"[{r['id']}] From: {r['from']} | Subject: {r['subject']}\n{r.get('body') or r['snippet']}"
                              for r in rows)
        try:
            resp = client.models.generate_content(
                model=REVIEW_MODEL,
                contents=_named("These emails are in Alex Moldovan's inbox. Which ones expect a reply from Alex? "
                                "Ignore automated mail, newsletters, and phishing or suspicious requests.\n\n", owner) + listing,
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   response_json_schema=json.loads(_named(json.dumps(NEEDS_REPLY_SCHEMA), owner))))
            return json.loads(resp.text)["needs_reply"]
        except Exception:  # a reviewer outage must not kill the run: skip the check, and say so in the log
            log.exception("needs-reply check failed; skipping it")
            return []
    return check


def gemini_reviewer(owner: str = "Alex Moldovan"):
    """Returns a review function, or None if no GEMINI_API_KEY is configured."""
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return None
    from google.genai import types
    client = _client()

    def review(draft: dict, thread_text: str) -> dict:
        try:
            resp = client.models.generate_content(
                model=REVIEW_MODEL,
                contents=_named(PROMPT, owner).format(thread=thread_text, to=draft["to"], body=draft["body"]),
                config=types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=json.loads(_named(json.dumps(SCHEMA), owner))),
            )
            return json.loads(resp.text)
        except Exception as e:  # shown on the review page, so Alex knows this draft was not reviewed
            log.exception("draft review failed")
            return {"commitments": [], "leaks": [], "recipient_ok": True, "note": f"NOT REVIEWED: {type(e).__name__}"}
    return review
