# Shared learnings (committed — ships with the harness)

Generic, **product- and stack-agnostic** lessons that have been human-**promoted** from the
local, gitignored `learnings/<agent>.md` store. Unlike that local store (machine-accumulated,
one per installation), everything in this directory is committed — so **every clone and every
project starts with these lessons**. They are injected into each agent's system prompt by
`learnings.augment_system`, alongside (and ahead of) the local store.

This tier is intentionally separate from:
- **`skills/<agent>.md`** — hand-authored, holistic domain knowledge. Keeping promoted machine
  lessons out of it means an auto-distilled lesson can never corrupt a curated skill.
- **`learnings/<agent>.md`** (local) — raw, machine-accumulated candidates that may be
  stack/product-specific and never leave the clone that learned them.

## How a lesson gets here

The end-of-run retro distils raw lessons into the **local** store. A human reviews them and
graduates the generic ones into this tier:

```bash
python -m tools.learnings list                       # review local candidates + shared, per agent
python -m tools.learnings promote --agent engineer \
    --index 3 \
    --as "State it as a transferable principle. (Default stack: …concrete example…)"
# or add a fresh, already-generic lesson directly:
python -m tools.learnings promote --agent qa --text "Test intent at the API/behavior level."
```

Promoting by `--index` **graduates** the candidate: it is removed from the local store so the
same lesson isn't injected from both tiers.

## The one rule

A lesson in this tier **must be usable for any product on any tech stack.** Keep stack
specifics as a parenthetical `(Default stack: …)` example — never as the lesson itself.

One file per agent: `<agent>.md`, a flat `- ` bullet list. Agents:
`pm`, `design`, `architect`, `test_author`, `engineer`, `qa`, `devops`.
