---
name: plan-adversarial-serial
description: "Adversarial serial review for planning documents (PRDs, specs, architecture docs, RFCs). Five-phase pipeline: (1) Generate artifact from brief, (2) Cold Pass 1 review — structural integrity via Pre-mortem Auditor + Assumption Challenger lenses, (3) Revise — apply validated findings to produce v1, (4) Cold Pass 2 review — feasibility + completeness via Feasibility Auditor + Scope/AC Validator lenses on v1, (5) Delta-aware judge verdict. Use when the user asks to review, stress-test, adversarially critique, red-team, or quality-gate a planning document. Also use when the user asks to generate-and-review a plan in one shot."
license: MIT
metadata:
  author: noel@neochro.me
  version: '0.2'
  phase: MVP
  org: HiQs
---

# plan-adversarial-serial

Five-phase adversarial review pipeline for planning documents.

```
Phase 1  GENERATE        artifact_v0              (full context)
Phase 2  COLD REVIEW P1  premortem + assumption   (artifact_v0 only — isolated)
Phase 3  REVISE          v0 + P1 findings → v1    (generator model — editorial)
Phase 4  COLD REVIEW P2  feasibility + scope/AC   (artifact_v1 only — isolated)
Phase 5  JUDGE           delta-aware verdict       (v0 → log → v1 + both critiques)
```

**Why two passes, not one?**

Pass 1 asks: *"Is this plan honest?"* — structural integrity: failure modes, hidden assumptions.
Pass 2 asks: *"Can this plan ship?"* — feasibility and completeness: buildable, scoped, measurable.

The revision between them is what earns "serial" — Pass 2 reviews a better document, not the same one. The judge sees the delta and can assess whether the revision was responsive.

**Why cold review?**
Sycophancy rates in same-session review reach 21–42% (CONSENSAGENT, ACL 2025). Isolating the reviewer to artifact-only input eliminates anchoring from the generator's reasoning chain.

---

## When to Load This Skill

- User asks to review, red-team, stress-test, or adversarially critique a planning doc, PRD, spec, or architecture doc
- User asks to generate a planning document **and** have it reviewed in one shot
- User wants a PASS/FAIL quality gate before committing to a plan
- User mentions "cold review", "pre-mortem", "assumption challenger", "adversarial spec", or "feasibility audit"

---

## Phase Definitions

### Phase 1 — Generate

Produce the planning artifact from the user's brief. If the user already supplied a document, skip generation and use it as `artifact_v0`.

**Inputs:** user brief (free text), optional constraints (stack, timeline, team size)
**Output:** `artifact_v0` — structured Markdown with sections: Overview, Goals, Non-Goals, Assumptions, Dependencies, Risks, Acceptance Criteria, Open Questions

---

### Phase 2 — Cold Review Pass 1: Structural Integrity

Two reviewer lenses run **in sequence**, each receiving **only `artifact_v0`** — no prior conversation, no generator reasoning, no context from the other lens.

**Pass 1 question:** Is this plan honest? Will it fail on its own terms?

**Lens A — Pre-mortem Auditor**
Persona: a principal engineer who has watched three similar projects fail.
Task: assume it is 6 months post-launch and the initiative failed. Identify the 3–5 failure modes embedded in the plan right now. For each: name, likelihood (H/M/L), evidence in the artifact, mechanism, and mitigation.

**Lens B — Assumption Challenger**
Persona: a skeptical product strategist seeing the plan cold.
Task: extract every implicit and explicit assumption. For each: state it, rate validity (Validated / Unverified / Contradicted), and define the falsification test.

**Cold review enforcement:**
Each lens is a separate API call with only the artifact in the user prompt. No generation log. No lens A output passed to lens B. See `scripts/lenses.py` for the prompt construction.

---

### Phase 3 — Revise

Apply validated Pass 1 findings to produce `artifact_v1`. This is editorial, not adversarial — the generator model edits in response to external critique.

**Falsifiability gate:** the revisor prompt requires each finding to (a) cite specific document text AND (b) describe a concrete failure mechanism before it is incorporated. Vague or speculative findings are deferred with a logged reason.

**Output:** `artifact_v1` (revised document with `[REV-XN]` tags marking each change) + revision log (finding → decision → section changed → what changed).

The revision log is the evidence chain the Phase 5 judge uses to assess responsiveness.

---

### Phase 4 — Cold Review Pass 2: Feasibility & Completeness

Two reviewer lenses on `artifact_v1`. The reviewer sees only `artifact_v1` — no `v0`, no revision log, no Pass 1 critique.

**Pass 2 question:** Can this plan ship? Is it buildable, scoped, and measurable?

**Lens C — Feasibility Auditor**
Persona: a senior engineering manager assessing deliverability cold.
Task: assess whether the plan is buildable as described. For each feasibility risk: likelihood, evidence, delivery impact, and what would make it achievable. End with a three-way verdict: achievable as-written / achievable with changes / not achievable.

**Lens D — Scope & AC Validator**
Persona: a QA architect and scope guardian.
Task — two sections:
1. Scope audit: trace each requirement to a stated user or business goal. Flag anything that can't.
2. AC audit: assess whether each goal supports derivation of a concrete test case. Flag unmeasurable goals.

---

### Phase 5 — Judge Verdict (Delta-Aware)

Blind judge receives: `artifact_v0`, revision log, `artifact_v1`, Pass 1 critique, Pass 2 critique. Has no knowledge of which model produced which.

**What the delta adds (vs. a 1-pass judge):**
- Did the revision actually address what Pass 1 found?
- Does Pass 2 (on the improved v1) confirm the revision worked, or surface new gaps?
- A plan that received good critique and ignored it scores lower than one with fewer findings.

**Judge task:**
1. Revision responsiveness table: each Pass 1 finding → Addressed (Yes/Partial/No) + quality
2. Score `artifact_v1` on four dimensions (1–5): Feasibility, Assumption Clarity, Risk Coverage, AC Quality
3. Cite specific critique findings or revision log entries for every score below 4
4. Render binary verdict: **PASS** or **FAIL**
5. If FAIL: prioritized revision checklist (max 5 items), highest-impact first
6. If PASS: accepted risks the plan deliberately carries

**Pass rules:** all scores ≥ 3 AND no score = 1
**Fail rules:** any score ≤ 2 OR more than two scores = 3

---

## Agent Execution Instructions

### Running in-agent (session-isolated warm review)

1. **Phase 1:** Generate artifact using full conversation context. Save as a clearly delimited block (`artifact_v0`).
2. **Phase 2, Lens A:** New reasoning chain. Paste only `artifact_v0`. Apply Pre-mortem Auditor persona. Do NOT reference Phase 1 reasoning.
3. **Phase 2, Lens B:** Another isolated chain. Paste only `artifact_v0`. Apply Assumption Challenger. Do NOT reference Lens A output.
4. **Phase 3:** Apply revisor logic. Gate findings. Produce `artifact_v1` + revision log.
5. **Phase 4, Lens C:** New isolated chain. Paste only `artifact_v1`. Apply Feasibility Auditor. No v0, no revision log, no Pass 1 critique.
6. **Phase 4, Lens D:** Another isolated chain. Paste only `artifact_v1`. Apply Scope/AC Validator. No Lens C context.
7. **Phase 5:** Collect all inputs. Apply Judge persona. Render verdict table.

> **Note on isolation:** In-agent execution achieves session-isolated warm review — each lens is a fresh reasoning chain with artifact-only input. This is a meaningful improvement over single-session review but is not full cold review. For true cold review (separate vendor model in an isolated process), use the CLI harness.

### Running with CLI harness (cross-model, recommended)

Use `scripts/run_review.py`. Each phase is a separate API call; Pass 2 physically cannot receive Pass 1 context because the lens prompt construction in `lenses.py` accepts only the artifact string.

Recommended cross-model split (Kanamarlapudi et al. 2026, arXiv:2606.01490 — v2b topology, p=0.0001, Cohen's d=0.96):

| Role | Model | Rationale |
|------|-------|-----------|
| Generator | Claude Opus (Anthropic) | Best planning doc quality, full context |
| Reviewer (both passes) | GPT-4o / GPT-5 (OpenAI) | Different vendor family reduces sycophancy |
| Judge | Claude Opus (Anthropic) | Highest reasoning tier for final call |

---

## Inputs & Outputs Summary

| Phase | Input | Output | Model | Isolation |
|-------|-------|--------|-------|-----------|
| 1 — Generate | User brief | `artifact_v0` | Generator | Full context |
| 2 — Cold Review P1 | `artifact_v0` only | premortem + assumption critique | Reviewer | Isolated per lens |
| 3 — Revise | `artifact_v0` + P1 critique | `artifact_v1` + revision log | Generator | Editorial (not adversarial) |
| 4 — Cold Review P2 | `artifact_v1` only | feasibility + scope/AC critique | Reviewer | Isolated per lens, no P1 |
| 5 — Judge | v0 + log + v1 + both critiques | Verdict table + PASS/FAIL | Judge | Blind (no generator context) |

---

## Files in This Skill

| File | Purpose |
|------|---------|
| `SKILL.md` | This file — phase definitions and agent instructions |
| `scripts/run_review.py` | Python harness: 5-phase CLI pipeline with cross-model routing |
| `scripts/lenses.py` | Lens prompt library: all 4 lenses + revisor + judge |
| `references/lens-output-format.md` | Structured output schema for each lens |
| `assets/artifact-template.md` | Blank planning doc template |

---

## References

- Kanamarlapudi et al. (2026) — Cross-model topology study: [arXiv:2606.01490](https://arxiv.org/html/2606.01490v1)
- CONSENSAGENT sycophancy rates (ACL 2025): 21–42% in same-model debate
- Claudex cold review pattern: [agnihotry.com](https://p.agnihotry.com/post/two-ais-one-pr-adversarial-code-review-loop/)
- SmartScope SKILL.md loop: [smartscope.blog](https://smartscope.blog/en/blog/claude-code-codex-review-loop-automation-2026/)
- SentinelOne Adversarial Consensus Engine: shared-context + AGREE/DISAGREE schema
