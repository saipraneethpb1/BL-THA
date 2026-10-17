A client had a follow-up meeting after approving a scope. You compare what they said in the new meeting with the requirements of the approved scope.

You get the approved requirements (ids like REQ-003) and the requirements extracted from the new meeting (ids like NEW-02), each with the client's words.

Return one link per relationship:
- `change: "same"`: a new-meeting item restates an approved requirement with no real change. Set both ids.
- `change: "modified"`: it changes an approved requirement (more or fewer options, a different behaviour, a different priority). Set both ids, say exactly what changed in `summary`, and write the complete requirement after the change in `revised_statement`. Clients usually state only the difference ("also show opening hours"), so keep every part of the approved requirement that still applies.
- `change: "added"`: a new need that no approved requirement covers. Set `new_item_id` only.
- `change: "removed"`: the client dropped an approved requirement ("we don't need the blog any more"). Set `baseline_id`, and `new_item_id` if a new-meeting item records the removal.

Approved requirements that the new meeting did not mention need no link: they stay as they are.

`constraint_changes`: changes to budget, deadline, launch date, sign-off or anything similar, each in one sentence with the old and new value when known.

Be conservative: only call something `removed` if the client clearly dropped it, and only `same` if nothing changed. Use only the ids given.
