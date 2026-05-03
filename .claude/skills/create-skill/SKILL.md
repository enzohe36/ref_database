---
name: create-skill
description: This skill should be used when the user asks to "create a skill", "add a new skill", "write a Claude Code skill", "author a skill", or wants to package reusable guidance as a SKILL.md under .claude/skills/. Covers frontmatter, directory layout, progressive disclosure, and when to include scripts or reference files.
---

## When this skill fires

The user asks you to package reusable instructions, a contract, or a runnable helper as a Claude Code skill. Typical phrasings: "create a skill for X", "write a SKILL.md that tells Claude to Y", "turn this checklist into a skill".

## Location

Project-local skills live under `.claude/skills/<skill-name>/`. The directory name IS the skill name, and it must match the `name:` field in the frontmatter.

## SKILL.md frontmatter

Every skill starts with YAML frontmatter between `---` markers:

```yaml
---
name: skill-name
description: This skill should be used when the user asks to "phrase one", "phrase two", or mentions <topic>. <One-line sentence on what the skill delivers.>
---
```

- **`name`** (required): lowercase letters, numbers, hyphens only. Max 64 chars. Must equal the directory name.
- **`description`** (required): third-person, trigger-phrase oriented, max 1024 chars. This is how the harness decides when to load the skill body — weak descriptions mean the skill never fires. Put the user phrases you actually expect to hear in quotes.
- **`paths`** (optional, project convention in this repo only): glob patterns for auto-activation when the user edits matching files. Not part of the official spec but supported by this project's harness.

Optional official fields (rarely needed):
- `version` — semver string.
- `disable-model-invocation: true` — suppresses automatic firing; the skill then only runs when the user types `/<skill-name>` explicitly.
- `allowed-tools` — space-separated list that restricts which tools Claude may call while the skill is active.

## Directory layout

```
.claude/skills/<skill-name>/
├── SKILL.md             # required — metadata + body
├── scripts/             # optional — runnable helpers the skill invokes
├── references/          # optional — long-form docs loaded on demand
├── examples/            # optional — full working examples
└── assets/              # optional — templates, images, other output resources
```

Only `SKILL.md` is required. Add subdirectories only when they earn their keep.

## Progressive disclosure

Three loading tiers — design the skill around them:

1. **Frontmatter (always in context).** Keep it under ~100 words. `description` is how Claude finds the skill.
2. **SKILL.md body (loads when the skill fires).** Target < 5000 words. Put contracts, decision rules, and invariants here.
3. **Bundled files (loaded on demand).** Reference them from the body with path hints like `scripts/foo.py` or `references/api.md`. Anything that's long, rarely needed, or machine-readable goes here — keeps the body compact.

Rule of thumb: if a chunk of content is needed every time the skill fires, it belongs in the body. If it's needed sometimes (a reference table, a long example), put it under `references/` or `examples/` and mention the path in the body.

## Writing the body

- State the goal in one or two sentences at the top.
- Lead with contracts and invariants, not prose. What must hold after the skill's work is done?
- Name the failure modes. What would a naive implementer get wrong? Call those out explicitly so Claude avoids them.
- When the skill has a mandatory verification step, say so with the word "mandatory" or "must" and describe exactly what to run.
- Avoid hedging. Skills are directives — soften language later if a rule turns out to be too strict.
- Keep examples minimal and correct. A bad example in a skill misleads every future run.

## When to include a script

Include a script under `scripts/` when the skill needs a repeatable programmatic check (e.g., render a page in a browser and assert its layout, validate a file's schema, probe an API). Scripts must:

- Run standalone from the repo root: `python .claude/skills/<name>/scripts/<file>.py <args>`.
- Accept input paths as CLI arguments — the skill body shouldn't embed hard-coded paper/file names.
- Print a machine-parseable summary (JSON or key=value lines) so Claude can read the result without a screenshot or OCR.
- Document their usage at the top of the file in a docstring.

Reference the script from the body with a one-line "To verify, run: `python .claude/skills/<name>/scripts/<file>.py <args>`" so the next run of the skill sees it immediately.

## Review before finishing

Before declaring a skill done, re-read your own SKILL.md and ask:

1. Would Claude know WHEN to fire this? (description has real user phrases, not just topics)
2. Would Claude know WHAT to do if it fires cold? (body is self-contained, no implicit session context)
3. Does the skill tell Claude how to verify its own output? (contract, check, or script)
4. Is anything in the body that belongs under `references/`, or vice versa?

If any answer is no, fix before handing back.
