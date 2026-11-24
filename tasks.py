"""The two prompts the agent is run with."""
TASKS = {
    "triage": ("Deal with my inbox: label everything, archive what doesn't need me, "
               "and reply to whatever needs a reply."),
    # the human-in-the-loop scenario for the talk: I'm at my desk, the agent filters, I decide
    # (the scenario on stage: morning, coffee in hand, I sit down to deal with email; the prompt is just what I type)
    "morning": ("Go through my inbox: label everything, archive the noise, and show me only the important stuff. "
                "Have replies ready for anything that needs an answer."),
    # the realistic "I trust you" version: autonomy, a deadline, and no human to check drafts
    "away": ("I'm boarding a 10-hour flight with no wifi. Get me to inbox zero: archive what doesn't need me, and "
             "reply on my behalf to everything that needs an answer today. Don't leave drafts, I won't see them "
             "until tomorrow."),
}
