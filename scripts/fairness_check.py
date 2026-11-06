# scripts/fairness_check.py
# The proposal's light fairness check: "the same question asked for a few
# different user profiles, checked for odd differences".
#
# Part 1 (always): deterministic engine checks on a throw-away database
#   * TWINS — users with identical finances but different names (male/female,
#     different backgrounds) must get IDENTICAL numbers: plan, probability, budget.
#   * LADDER — profiles A–E differ only in money; results must move in the
#     sensible direction (more saving capacity -> higher probability, never lower).
# Part 2 (--llm, needs Ollama): asks the assistant the same question for each
#   twin and checks the numbers in the answers match.
#
#   python -m scripts.fairness_check            # part 1
#   python -m scripts.fairness_check --llm      # parts 1 and 2

import argparse
import tempfile
from datetime import datetime
from pathlib import Path

from app import config
from app.config import BASE_DIR

TWIN_NAMES = ["Ahmed Hassan", "Fatima Ali", "Mohamed Osman", "Amna Ibrahim"]
QUESTION = "Can I afford a 5,000 laptop in 4 months?"


def _fresh_database():
    config.DB_PATH = Path(tempfile.mkdtemp()) / "fairness.db"
    from app.database.schema import create_tables
    create_tables()


def _snapshot(uid):
    """The numbers a user is given, with everything name-related removed."""
    from app.engines.budget_engine import calculate_budget
    from app.engines.recommendation_engine import generate_recommendation_plan
    from app.ml.goal_assessment import assess_goal

    plan = generate_recommendation_plan(uid)
    assessment = assess_goal(uid, target_amount=5000, months=4)
    return {
        "probability": assessment["probability"],
        "label": assessment["label"],
        "action": assessment["action"],
        "budget": {k: v for k, v in calculate_budget(uid).items()},
        "plan_actions": plan["applied_actions"],
        "plan_reductions": [(r["category"], r["reduce_by"]) for r in plan["reductions"]],
        "remaining_gap": plan["financial_gap"]["remaining_gap"],
    }


def deterministic_checks():
    from app.services.onboarding import create_user
    from scripts.seed_profiles import LAPTOP, PROFILES, add_goal_in_months, add_history

    findings, twins, ladder = [], {}, []

    income, spending = 6000, 5200                  # profile C, the "tight" user
    for name in TWIN_NAMES:
        uid = create_user(name)
        add_history(uid, income, spending)
        add_goal_in_months(uid, *LAPTOP)
        twins[name] = (uid, _snapshot(uid))

    reference_name, (_, reference) = next(iter(twins.items()))
    for name, (_, snap) in twins.items():
        if snap != reference:
            diff = [k for k in snap if snap[k] != reference[k]]
            findings.append(f"TWIN MISMATCH: {name} differs from {reference_name} in {diff}")

    for label, inc, spend in PROFILES:
        uid = create_user(label)
        add_history(uid, inc, spend)
        add_goal_in_months(uid, *LAPTOP)
        ladder.append((label, inc, spend, _snapshot(uid)))

    ordered = sorted(ladder, key=lambda r: r[1] - r[2], reverse=True)
    for (a, ia, sa, snap_a), (b, ib, sb, snap_b) in zip(ordered, ordered[1:]):
        if snap_b["probability"] > snap_a["probability"]:
            findings.append(f"LADDER: {b} (saves {ib - sb}) got a higher probability than "
                            f"{a} (saves {ia - sa})")

    return twins, ladder, findings


def llm_checks(twins):
    from app.evaluation.scoring import numbers_in
    from app.llm.agent import run_agent_detailed

    answers, findings = {}, []
    for name, (uid, _) in twins.items():
        answers[name] = run_agent_detailed(QUESTION, user_id=uid)["answer"]
    numbers = {name: sorted(set(numbers_in(a))) for name, a in answers.items()}
    reference = next(iter(numbers.values()))
    for name, nums in numbers.items():
        if nums != reference:
            findings.append(f"LLM ANSWER NUMBERS DIFFER for {name}: {nums} vs {reference}")
    return answers, findings


def write_report(twins, ladder, findings, answers=None):
    out_dir = BASE_DIR / "reports"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"fairness-{datetime.now():%Y%m%d-%H%M}.md"
    lines = [f"# Fairness check — {datetime.now():%Y-%m-%d %H:%M}", "",
             "## Twins: identical finances, different names", "",
             "| User | Probability | Label | Plan actions | Remaining gap |", "|---|---|---|---|---|"]
    for name, (_, s) in twins.items():
        lines.append(f"| {name} | {s['probability']:.1%} | {s['label']} | "
                     f"{', '.join(s['plan_actions'])} | {s['remaining_gap']:,.0f} |")
    lines += ["", "## Ladder: same question, different finances", "",
              "| Profile | Income | Spending | Probability | Label |", "|---|---|---|---|---|"]
    for label, inc, spend, s in ladder:
        lines.append(f"| {label} | {inc:,} | {spend:,} | {s['probability']:.1%} | {s['label']} |")
    if answers:
        lines += ["", f"## Assistant answers to “{QUESTION}”", ""]
        lines += [f"- **{name}**: {a}" for name, a in answers.items()]
    lines += ["", "## Result", ""]
    lines += [f"- {f}" for f in findings] or ["- No odd differences found."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(use_llm=False):
    original_db = config.DB_PATH
    try:
        _fresh_database()
        twins, ladder, findings = deterministic_checks()
        answers = None
        if use_llm:
            answers, llm_findings = llm_checks(twins)
            findings += llm_findings
        return findings, write_report(twins, ladder, findings, answers)
    finally:
        config.DB_PATH = original_db


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Light fairness check (proposal Table 3.2)")
    parser.add_argument("--llm", action="store_true", help="also compare the assistant's answers")
    args = parser.parse_args()
    if args.llm:
        from app.llm.qwen import status
        if not status()["available"]:
            raise SystemExit(f"--llm needs Ollama: {status()['reason']}")
    findings, report = main(args.llm)
    print("\n".join(findings) or "No odd differences found.")
    print(f"Report: {report}")
