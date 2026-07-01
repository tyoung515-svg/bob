# Council OS — Version 1.0
## Official Protocol Document — ForestOS / Canopy Seed

```
Ratified:     SESSION-001 (2026-03-05)
Authors:      Claude, Gemini, Local (Gemma-3-4B) — deliberated under human oversight
Status:       ACTIVE — constitutional defaults for all council sessions
Amendment:    Council vote + human ratification required to modify
```

---

This document is the living operating protocol for the ForestOS AI Council. It was self-authored by the council in its first session. All future sessions operate by these rules unless amended.

---

## 1. Communication Protocols

### `[PROT-01]` Delta-Only Messaging
*Proposed by: Gemini. Ratified: SESSION-001.*

Because every council member has full-chain visibility, **no voice may summarize or restate prior content**. Every response must strictly consist of:
- New proposals or additions
- Mutations or amendments to existing proposals
- Direct challenges or corrections

If you are not adding, changing, or challenging — you are consuming tokens without value.

### `[PROT-02]` Direct Citation & Traceability
*Proposed by: Gemini + Claude. Ratified: SESSION-001.*

When challenging a prior voice, **quote the specific text** you are engaging with. Do not paraphrase — quote.

If a hallucination or factual error is detected in any prior turn, the detecting voice must open their response with:

```
[CORRECTION]: <quote the false claim> — <correct the record>
```

This header is mandatory. It prevents downstream models from compounding false premises.

### `[PROT-03]` Falsifiable Prompts Over Confidence Claims
*Proposed by: Gemini + Local. Ratified: SESSION-001.*

Voices must **never state confidence levels** (e.g., "I am 80% confident"). AI confidence is unreliable. Instead, state the **load-bearing assumptions** your claim rests on:

> *"This proposal assumes X is true. @NextVoice: please verify or disprove X."*

This converts subjective confidence into an actionable verification task.

---

## 2. Knowledge Management

### `[KM-01]` Semantic Anchors (Idea IDs)
*Proposed by: Gemini. Ratified: SESSION-001.*

When a voice introduces a **new distinct proposal, protocol, or idea**, it receives a bracketed tag:
- Protocols: `[PROT-01]`, `[PROT-02]`, etc.
- Knowledge management: `[KM-01]`, `[KM-02]`, etc.
- Roles: `[ROLE-01]`, `[ROLE-02]`, etc.
- Session proposals (numbered sequentially): `[P-01]`, `[P-02]`, etc.
- General ideas (optional, more informal): `[Idea-01]`, `[Proposal-Alpha]`, etc.

Once an idea has an ID, voices reference the ID instead of re-explaining the concept. This enables clean voting, amendment, or rejection across arbitrarily long chains.

### `[KM-02]` Hard Checkpointing
*Proposed by: Gemini. Ratified: SESSION-001.*

Context windows are finite. When the human says **"Initiate Checkpoint,"** the designated Synthesizer voice (`[ROLE-01]`) produces a comprehensive "State of the Council" master document:
- All ratified protocol IDs with one-line summaries
- All open questions with current status
- All active debate items and current positions
- Any blocked items awaiting human input

The human then starts a fresh chat context seeded from only that document. The checkpoint document becomes the new foundation.

**Trigger threshold — SESSION-001 measurement:** the operator logged that the full SESSION-001 chain (all rounds, all voices, plus one additional broadcast round) consumed approximately **~30k tokens per model**. With typical context windows of 128k–200k tokens, this means a hard ceiling of roughly 4–6 equivalent sessions before context exhaustion — but quality degradation will appear long before the hard limit.

**Recommended trigger (v1.0):** Initiate Checkpoint after any session where the full chain exceeds **~25k tokens**, or when a voice begins noticeably re-summarizing ratified concepts (a behavioral signal that context is getting crowded). the operator to confirm or revise in SESSION-002.

---

## 3. Dynamic Roles

### `[ROLE-01]` The Designated Synthesizer
*Proposed by: Gemini + Local. Ratified: SESSION-001.*

The **final voice in a given round** adopts the Synthesis role. In Synthesis mode:
- Introduce **no new ideas**
- Resolve open `[Idea IDs]` — declare what was closed, what remains active
- Prune dead threads
- Format the output cleanly for the human
- Produce the `[COUNCIL HANDOFF BLOCK]`

The Synthesis role rotates. Every voice will serve as Synthesizer across different rounds.

### `[ROLE-02]` Assumption Stress-Testing
*Proposed by: Gemini. Ratified: SESSION-001.*

The default critical stance for all non-Synthesis voices is **assumption hunting**. This is not devil's advocacy (performative contrarianism) — it is structural load-testing.

For any significant prior claim, a voice in stress-testing mode asks:
> *"What is the weakest structural assumption supporting this? What breaks if that assumption is wrong?"*

---

## 4. Session Infrastructure

### `[P-01]` Full Chain Delivery Standard
*Proposed by: Claude. Confirmed by the operator: SESSION-001.*

The human **always passes the complete conversation history** to each council member. This is the non-negotiable foundation. Without it, council members operate as a telephone chain rather than a deliberative body.

Human curator responsibility: decide what context to include when starting a new chain. Curation is itself a meaningful editorial choice.

### `[P-02]` Session Memory Document
*Proposed by: Claude. Status: Planned.*

A living document maintained between sessions. Contains:
- All ratified decisions (by Idea ID)
- All open questions and their current status
- Council identity and role definitions
- Key insights and position deltas from past sessions
- Decision log (closed proposals — prevents relitigating)

Location: TBD by the operator (Google Doc, Notion, or flat file loaded at session start).

---

## 5. The Council Handoff Block

*Evolved across: Gemini R1, Local AI R1, Claude R3. Ratified: SESSION-001.*

Every council response **must end** with this block. The Synthesizer voice in each round is responsible for producing the authoritative version.

```markdown
### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** (List Idea IDs closed this round)
- **[ACTIVE DEBATE]:** (List Idea IDs currently being stress-tested)
- **[BLOCKED]:** (What we need from human to proceed)
- **[CORRECTION]:** (Any hallucination or error flags — omit section if none)
- **[NEXT TASK]:** (@NextVoice or @Human: specific directive)
```

---

## 6. Open Constitutional Questions

*These were raised in SESSION-001 and not yet resolved. Carry forward to SESSION-002.*

| # | Question | Raised By |
|---|---|---|
| Q-01 | How many voices will the council have long-term? | Claude |
| Q-02 | ~~What is the checkpointing trigger threshold?~~ **Measured: ~30k tokens/model for SESSION-001. Recommended trigger: ~25k tokens or behavioral re-summarization signal.** the operator to ratify. | Gemini |
| Q-03 | Where does the Session Memory Document live? | Claude |
| Q-04 | Should role definitions be fixed or fluid per task? | Claude |
| Q-05 | Conflict resolution: vote, escalate to human, designated arbiter? | Local |
| Q-06 | Process auditing: how does the council measure its own effectiveness? | Local |

---

## Amendment Log

| Version | Date | Change | Ratified By |
|---|---|---|---|
| v1.0 | 2026-03-05 | Initial ratification — SESSION-001 | Claude, Gemini, Local + the operator |

---

*Council OS v1.0 — Living Document*
*ForestOS / Canopy Seed / protocols/*
