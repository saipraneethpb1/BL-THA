# Meeting transcripts

> **What is actually used.** No role-play partner was available inside the 4-day window, so the evaluated meetings are three real recorded meetings from the public **AMI Meeting Corpus** (ES2002b, ES2002c and its follow-up ES2002d; see `AMI_ATTRIBUTION.md`). The guide below is how meetings *would* be recorded for a real client project.

The brief requires at least three role-played client meetings, recorded with a friend. One must be a follow-up that changes the first. Put them here as `.txt` files. Only real recordings go in this folder; test fixtures live in `tests/`.

## File format

One line per speaker turn. Any of these work:

```
[00:12:31] Priya (Client): We need filters on the search page.
Priya (00:12:31): We need filters on the search page.
Priya: We need filters on the search page.
```

Lines without a speaker are joined onto the previous turn. Most transcription tools (Otter, Zoom, Google Meet, Whisper) export something close to this; fix the speaker names, then save.

Name the files `meeting_a_YYYY-MM-DD.txt` and so on, and add one line to `meetings.json` saying which project each meeting belongs to.

## Suggested scenarios

Aim for 15–20 minutes each. Long is fine: the pipeline is built for long transcripts.

**Meeting A: directory search and discovery.** A client runs a directory, for example vets or tutors. They say "the search is bad", want filters, maybe "something with AI", and a map. To test the system, include:
- something the product already has (the map, search sorting, multiple categories via the workaround);
- something vague ("make it better");
- a budget and a launch date;
- a promise from each side ("I'll send the category list").

**Meeting B: e-commerce workflow.** A different client wants checkout changes, order notification emails and a payment integration. Include an external dependency the client controls (their Stripe account, an accountant's sign-off), and ask whether a payment provider is supported.

**Meeting C: follow-up to A.** Two weeks later, the same client:
- adds one new requirement;
- drops one feature;
- changes one detail (for example, filters by price as well as location);
- moves the launch date.

This is what tests scope comparison and out-of-scope flagging.

## Before you record

Get your friend's consent to record, and use made-up company names. Speaking loosely, the way real clients do, makes the test better.
