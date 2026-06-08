#!/usr/bin/env python3
"""
plan-adversarial-serial — v0.2 harness
5-phase serial adversarial review pipeline for planning documents.

Phase flow
----------
  1  GENERATE        Generate artifact from brief (skip if --artifact supplied)
  2  COLD REVIEW P1  Pre-mortem + Assumption Challenger on v0 (reviewer model)
  3  REVISE          Apply validated P1 findings → v1 + revision log (generator model)
  4  COLD REVIEW P2  Feasibility + Scope/AC Validator on v1 (reviewer model)
  5  JUDGE           Delta-aware verdict: v0 → log → v1 + both critiques (judge model)

What makes 2 passes meaningful, not redundant
---------------------------------------------
  Pass 1 asks: "Is this plan honest?"   (structural integrity)
  Pass 2 asks: "Can this plan ship?"    (feasibility + completeness)

  The revision between them is the feedback loop. Pass 2 reviews a better document.
  The judge sees the delta — it can tell whether the revision actually worked.

Usage
-----
  # Generate from brief, then full pipeline:
  python run_review.py --brief brief.txt

  # Review existing artifact (skip Phase 1):
  python run_review.py --artifact artifact.md

  # Explicit model control:
  python run_review.py --artifact artifact.md \\
    --generator-model claude-opus-4-6 \\
    --reviewer-model gpt-4o \\
    --judge-model claude-opus-4-6

Model selection
---------------
  Recommended cross-model split (Kanamarlapudi et al. 2026, arXiv:2606.01490):
    Generator  : Claude (Anthropic)        — full context, best planning doc quality
    Reviewer   : OpenAI GPT-4o or GPT-5   — different vendor reduces sycophancy
    Judge      : Claude Opus               — highest reasoning tier for final call

  The reviewer model is used for both Pass 1 and Pass 2 (same vendor isolation applies).
  Generator model is also used for the Phase 3 revision (author editing their own work).

Env vars
--------
  ANTHROPIC_API_KEY   for Claude (generator / judge)
  OPENAI_API_KEY      for OpenAI models (reviewer)
  GEMINI_API_KEY      optional, for Gemini reviewer

Cold review contract
--------------------
  Phase 2 reviewers receive ONLY artifact_v0.
  Phase 4 reviewers receive ONLY artifact_v1.
  Neither pass sees the generator's chain-of-thought, Phase 1 generation log,
  the other pass's critique, or the revision log.
  This is enforced structurally: all lens prompts in lenses.py accept only the
  artifact string — no additional context is passed.
"""

import argparse
import json
import os
import sys
import textwrap
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lenses import (
    assumption_challenger,
    feasibility_auditor,
    judge,
    premortem_auditor,
    revisor,
    scope_ac_validator,
)

# ── Optional SDK imports ─────────────────────────────────────────────────────
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


# ── Model routing ────────────────────────────────────────────────────────────

ANTHROPIC_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
}

OPENAI_MODELS = {
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "o3",
    "o4-mini",
}

DEFAULT_GENERATOR_MODEL = "claude-opus-4-6"
DEFAULT_REVIEWER_MODEL = "gpt-4o"
DEFAULT_JUDGE_MODEL = "claude-opus-4-6"


def call_llm(model: str, system: str, user: str) -> str:
    """Route to the correct SDK based on model name prefix."""
    m = model.lower()
    if m in ANTHROPIC_MODELS or m.startswith("claude"):
        return _call_anthropic(model, system, user)
    elif m in OPENAI_MODELS or m.startswith(("gpt", "o3", "o4")):
        return _call_openai(model, system, user)
    else:
        raise ValueError(
            f"Unknown model '{model}'. Add it to ANTHROPIC_MODELS or OPENAI_MODELS."
        )


def _call_anthropic(model: str, system: str, user: str) -> str:
    if not HAS_ANTHROPIC:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text
    except Exception as e:
        raise RuntimeError(f"Anthropic API call failed for model '{model}': {e}") from e


def _call_openai(model: str, system: str, user: str) -> str:
    if not HAS_OPENAI:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = OpenAI(api_key=key)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=4096,
        )
        return resp.choices[0].message.content
    except Exception as e:
        raise RuntimeError(f"OpenAI API call failed for model '{model}': {e}") from e


# ── Phase 1: Generate ────────────────────────────────────────────────────────

GENERATE_SYSTEM = """\
You are a senior technical product manager and solutions architect.
Your job is to produce a thorough, actionable planning document from a project brief.

Output format — use these sections exactly:
# [Project Name]

## Overview
## Goals
## Non-Goals
## Assumptions
## Dependencies
## Risks
## Acceptance Criteria
## Open Questions

Be specific. Avoid vague language. Every assumption must be stated explicitly.
Every acceptance criterion must be testable. Thin sections invite adversarial critique.
"""

def phase1_generate(brief: str, model: str) -> str:
    print(f"\n{'='*60}")
    print(f"PHASE 1 — GENERATE  [{model}]")
    print(f"{'='*60}")
    artifact = call_llm(model, GENERATE_SYSTEM, f"Generate a planning document for this brief:\n\n{brief}")
    print(artifact[:400] + "..." if len(artifact) > 400 else artifact)
    return artifact


# ── Phase 2: Cold Review Pass 1 ──────────────────────────────────────────────

def phase2_cold_review(artifact_v0: str, reviewer_model: str) -> tuple[str, str]:
    """
    Two lenses on artifact_v0. Each call receives only the artifact — no shared context.
    Returns (premortem_output, assumption_output).
    """
    print(f"\n{'='*60}")
    print(f"PHASE 2 — COLD REVIEW PASS 1  [{reviewer_model}]")
    print(f"{'='*60}")

    print("\n[Lens A] Pre-mortem Auditor ...")
    pm_prompt = premortem_auditor(artifact_v0)
    premortem_output = call_llm(reviewer_model, pm_prompt["system"], pm_prompt["user"])
    print(premortem_output[:300] + "..." if len(premortem_output) > 300 else premortem_output)

    print("\n[Lens B] Assumption Challenger ...")
    ac_prompt = assumption_challenger(artifact_v0)
    assumption_output = call_llm(reviewer_model, ac_prompt["system"], ac_prompt["user"])
    print(assumption_output[:300] + "..." if len(assumption_output) > 300 else assumption_output)

    return premortem_output, assumption_output


# ── Phase 3: Revise ──────────────────────────────────────────────────────────

def phase3_revise(
    artifact_v0: str,
    premortem_output: str,
    assumption_output: str,
    generator_model: str,
) -> tuple[str, str]:
    """
    Revise artifact_v0 based on Pass 1 critique.
    Runs with the generator model — this is editorial (author responding to critics),
    not adversarial. The revisor prompt applies a falsifiability gate before revising.

    Returns (artifact_v1, revision_log).
    The revisor prompt outputs a delimited block; we split on '## REVISION LOG'.
    """
    print(f"\n{'='*60}")
    print(f"PHASE 3 — REVISE  [{generator_model}]")
    print(f"{'='*60}")

    combined_critique = (
        "## Pre-mortem Findings\n\n" + premortem_output +
        "\n\n## Assumption Findings\n\n" + assumption_output
    )
    rev_prompt = revisor(artifact_v0, combined_critique)
    raw = call_llm(generator_model, rev_prompt["system"], rev_prompt["user"])

    # Parse on the explicit delimiter from the revisor prompt
    delimiter = "## REVISION LOG"
    if delimiter in raw:
        doc_part, log_part = raw.split(delimiter, 1)
        # Strip the "## REVISED DOCUMENT" header if the model included it
        artifact_v1 = doc_part.replace("## REVISED DOCUMENT", "").strip()
        revision_log = delimiter + "\n" + log_part.strip()
    else:
        # Fallback: model didn't use delimiter cleanly
        artifact_v1 = raw.strip()
        revision_log = "(Model did not produce a structured revision log — full output used as artifact_v1)"

    print(f"  artifact_v1: {len(artifact_v1)} chars")
    print(f"  revision_log: {len(revision_log)} chars")
    return artifact_v1, revision_log


# ── Phase 4: Cold Review Pass 2 ──────────────────────────────────────────────

def phase4_cold_review_pass2(artifact_v1: str, reviewer_model: str) -> tuple[str, str]:
    """
    Two lenses on artifact_v1. Reviewer sees only v1 — no v0, no revision log, no Pass 1 critique.
    Different questions from Pass 1: feasibility and completeness, not structural integrity.
    Returns (feasibility_output, scope_ac_output).
    """
    print(f"\n{'='*60}")
    print(f"PHASE 4 — COLD REVIEW PASS 2  [{reviewer_model}]")
    print(f"{'='*60}")

    print("\n[Lens C] Feasibility Auditor ...")
    fa_prompt = feasibility_auditor(artifact_v1)
    feasibility_output = call_llm(reviewer_model, fa_prompt["system"], fa_prompt["user"])
    print(feasibility_output[:300] + "..." if len(feasibility_output) > 300 else feasibility_output)

    print("\n[Lens D] Scope & AC Validator ...")
    sc_prompt = scope_ac_validator(artifact_v1)
    scope_ac_output = call_llm(reviewer_model, sc_prompt["system"], sc_prompt["user"])
    print(scope_ac_output[:300] + "..." if len(scope_ac_output) > 300 else scope_ac_output)

    return feasibility_output, scope_ac_output


# ── Phase 5: Judge (delta-aware) ─────────────────────────────────────────────

def phase5_judge(
    artifact_v0: str,
    revision_log: str,
    artifact_v1: str,
    premortem_output: str,
    assumption_output: str,
    feasibility_output: str,
    scope_ac_output: str,
    judge_model: str,
) -> str:
    """
    Blind judge receives everything. Key addition over 1-pass judge:
    - Sees v0 → revision log → v1 (the delta)
    - Assesses whether the revision was responsive to Pass 1
    - Pass 2 critique is on v1 (the improved document)
    - Can detect: "revision addressed structural issues but exposed feasibility gaps"
    """
    print(f"\n{'='*60}")
    print(f"PHASE 5 — JUDGE VERDICT  [{judge_model}]")
    print(f"{'='*60}")

    pass1 = (
        "### Pre-mortem Analysis\n\n" + premortem_output +
        "\n\n### Assumption Analysis\n\n" + assumption_output
    )
    pass2 = (
        "### Feasibility Analysis\n\n" + feasibility_output +
        "\n\n### Scope & AC Analysis\n\n" + scope_ac_output
    )

    j_prompt = judge(artifact_v0, revision_log, artifact_v1, pass1, pass2)
    verdict = call_llm(judge_model, j_prompt["system"], j_prompt["user"])
    print(verdict)
    return verdict


# ── Report bundling ──────────────────────────────────────────────────────────

def write_report(
    artifact_v0: str,
    artifact_v1: str,
    revision_log: str,
    premortem: str,
    assumption: str,
    feasibility: str,
    scope_ac: str,
    verdict: str,
    out_path: Path,
    meta: dict,
) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = textwrap.dedent(f"""\
        # Adversarial Review Report
        _Generated: {ts}_

        | Role | Model |
        |------|-------|
        | Generator | {meta['generator_model']} |
        | Reviewer | {meta['reviewer_model']} |
        | Judge | {meta['judge_model']} |

        ---

        ## Phase 1 — Planning Artifact (v0)

        {artifact_v0}

        ---

        ## Phase 2 — Cold Review Pass 1: Structural

        ### Pre-mortem Analysis (Lens A)

        {premortem}

        ### Assumption Analysis (Lens B)

        {assumption}

        ---

        ## Phase 3 — Revision

        {revision_log}

        ### Revised Artifact (v1)

        {artifact_v1}

        ---

        ## Phase 4 — Cold Review Pass 2: Feasibility & Completeness (on v1)

        ### Feasibility Analysis (Lens C)

        {feasibility}

        ### Scope & AC Analysis (Lens D)

        {scope_ac}

        ---

        ## Phase 5 — Judge Verdict

        {verdict}

        ---

        _plan-adversarial-serial v0.2 · HiQs · arXiv:2606.01490_
    """)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n✓ Report written to: {out_path}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="plan-adversarial-serial v0.2: 5-phase planning doc review pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Phase flow:
              1  GENERATE        artifact_v0              (generator model)
              2  COLD REVIEW P1  pre-mortem + assumption  (reviewer model, v0 only)
              3  REVISE          v0 + P1 critique → v1    (generator model)
              4  COLD REVIEW P2  feasibility + scope/AC   (reviewer model, v1 only)
              5  JUDGE           delta-aware verdict       (judge model)

            Examples:
              python run_review.py --brief brief.txt
              python run_review.py --artifact artifact.md
              python run_review.py --artifact artifact.md \\
                --reviewer-model gpt-4o --judge-model claude-opus-4-6
        """),
    )
    parser.add_argument("--brief", help="Path to project brief (Phase 1 input)")
    parser.add_argument("--artifact", help="Path to existing planning doc (skip Phase 1)")
    parser.add_argument("--out", default="review-report.md", help="Output report path")
    parser.add_argument("--generator-model", default=DEFAULT_GENERATOR_MODEL)
    parser.add_argument("--reviewer-model", default=DEFAULT_REVIEWER_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--json", action="store_true", help="Also write machine-readable JSON")

    args = parser.parse_args()

    if not args.brief and not args.artifact:
        parser.error("Provide --brief or --artifact")
    if args.brief and args.artifact:
        parser.error("Provide --brief OR --artifact, not both")

    out_path = Path(args.out)
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1
    if args.brief:
        brief_text = Path(args.brief).read_text(encoding="utf-8")
        artifact_v0 = phase1_generate(brief_text, args.generator_model)
        v0_path = out_dir / "artifact-v0.md"
        v0_path.write_text(artifact_v0, encoding="utf-8")
        print(f"  Saved: {v0_path}")
    else:
        artifact_v0 = Path(args.artifact).read_text(encoding="utf-8")
        print(f"  Loaded: {args.artifact} ({len(artifact_v0)} chars)")

    # Phase 2
    premortem, assumption = phase2_cold_review(artifact_v0, args.reviewer_model)

    # Phase 3
    artifact_v1, revision_log = phase3_revise(
        artifact_v0, premortem, assumption, args.generator_model
    )
    v1_path = out_dir / "artifact-v1.md"
    v1_path.write_text(artifact_v1, encoding="utf-8")
    print(f"  Saved: {v1_path}")

    # Phase 4
    feasibility, scope_ac = phase4_cold_review_pass2(artifact_v1, args.reviewer_model)

    # Phase 5
    verdict = phase5_judge(
        artifact_v0, revision_log, artifact_v1,
        premortem, assumption,
        feasibility, scope_ac,
        args.judge_model,
    )

    # Report
    meta = {
        "generator_model": args.generator_model if args.brief else f"n/a — loaded from {args.artifact}",
        "reviewer_model": args.reviewer_model,
        "judge_model": args.judge_model,
    }
    write_report(
        artifact_v0, artifact_v1, revision_log,
        premortem, assumption,
        feasibility, scope_ac,
        verdict, out_path, meta,
    )

    if args.json:
        json_path = out_path.with_suffix(".json")
        json_path.write_text(
            json.dumps({
                "meta": meta,
                "artifact_v0": artifact_v0,
                "pass1": {"premortem": premortem, "assumption": assumption},
                "revision": {"log": revision_log, "artifact_v1": artifact_v1},
                "pass2": {"feasibility": feasibility, "scope_ac": scope_ac},
                "verdict": verdict,
            }, indent=2),
            encoding="utf-8",
        )
        print(f"✓ JSON written to: {json_path}")


if __name__ == "__main__":
    main()
