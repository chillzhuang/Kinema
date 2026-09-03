# Kinema Agent Control Plane

[English](README.md) · [简体中文](README.zh-CN.md)

This directory holds the **metadata and contracts** of the Kinema Agent/Skill
control plane. Skill bodies live in exactly one place — the discovery directory
`.claude/skills/` — and are edited there directly (single source, no copies).
This directory tells the compiler and the engine what those skills *are*:
registry, permissions, routing, machine contracts and host entry pointers.

```text
agent/manifest.json + agent/contracts.json + agent/adapters/ + .claude/skills/ (bodies)
                         │
                         ▼
              python3 tools/agent_assets.py compile
                         │
          ┌──────────────┼──────────────────┐
          ▼              ▼                  ▼
 .claude/skills/    runtime catalog     host adapters
 frontmatter +      + contracts         + docs/skills/INDEX.md
 skill.json         (engine-side)
```

## What lives here (editable source)

| Path | Role |
|---|---|
| `manifest.json` | **The list of all skills and their paths**, plus governance: name, description, kind, status, dependencies, abstract permissions, budgets, output map. Each entry's `source` points at `.claude/skills/<id>` |
| `manifest.schema.json` | Public structure reference; the zero-dependency compiler enforces the same rules plus stricter cross-field semantics |
| `contracts.json` | Machine contracts (PromptSpec / ChapterPlan) the Agent Gateway serves |
| `adapters/` | Host-specific entry pointers (Claude Code / Cursor / Copilot); pointers only, no rule content |

Skill bodies (`SKILL.md` prose and `references/`) are **not** here — edit them in
`.claude/skills/<id>/` directly.

## Machine-maintained artifacts — DO NOT EDIT

The compiler derives the following from `manifest.json`, `contracts.json` and the
skill bodies. Hand edits are overwritten by the next `compile` and rejected by
`check` as drift.

| Generated artifact | What it is | Consumed by |
|---|---|---|
| `.claude/skills/<id>/SKILL.md` **frontmatter** | name/description/kind/status + `kinema-owner/source/trust/digest`, all derived from the manifest; the digest covers the body and `references/` so an uncompiled edit is caught | Every skill-standard host |
| `.claude/skills/<id>/skill.json` | Per-skill projection of the manifest entry + digest | External tooling |
| `engine/kinema/_generated/agent_catalog.json` | Runtime skill/profile catalog | `engine/kinema/skills.py` · `agent_system.py` (routing, Studio SKILL board) |
| `engine/kinema/_generated/agent_contracts.json` | Compiled machine contracts | `kinema agent contract` gateway |
| `.claude/skills/kinema/references/prompt-contract.md` | Human-readable rendering of `contracts.json` | Agent authors |
| `CLAUDE.md` (repo root) | Host pointer, from `adapters/CLAUDE.md` | Claude Code |
| `.cursor/rules/kinema.mdc` | Host pointer, from `adapters/cursor.mdc` | Cursor |
| `.github/copilot-instructions.md` | Host pointer, from `adapters/copilot-instructions.md` | GitHub Copilot |
| `docs/skills/INDEX.md` | Tool-neutral, human-readable skill index | Non-adopting tools |

`.agents/skills` is a symlink alias → `.claude/skills` for Codex / Gemini CLI /
Amp / OpenCode (agentskills.io standard). Repair on Windows:
`python tools/agents_alias.py`.

## Editing protocol

1. Skill prose and `references/`: edit `.claude/skills/<id>/` directly.
2. Skill name, description, kind, status, profile, dependencies, permissions —
   and adding or removing a skill: edit `manifest.json`; the compiler generates
   all frontmatter and `skill.json`.
3. Host differences: edit `agent/adapters/` only. Engineering discipline lives in
   the root `AGENTS.md`; module detail docs live in `docs/agents/`.
4. After every edit (either side):

   ```bash
   python3 tools/agent_assets.py compile
   python3 tools/agent_assets.py check
   cd engine && python3 -m unittest discover -s tests
   ```

## Version policy

`catalog_version` in `manifest.json` is a product decision, currently pinned at
`2.0.0`: it does not bump for architecture evolution or skill-content changes
before an official release. A bump is made explicitly by the product owner with
its own commit rationale. Every generated artifact (frontmatter,
`agent_catalog.json`) derives its version from it — never edit a version inside
a generated file.

## Architecture boundaries

- The Agent owns research, creative judgement, storyboarding and human-checkpoint
  collaboration; the engine owns deterministic directories, routing, validation
  and media execution, with no built-in LLM provider.
- Routing priority is fixed: project binding > explicit skill > explicit profile > `kinema`.
- The public manifest declares abstract, minimal permissions only — no
  Claude/Codex/Cursor host-specific permission fields.
- `stable` accepts first-party sources from this repository only; unregistered
  packages in the discovery directory, path escapes and symlinks are rejected at
  compile time.
