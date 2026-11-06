# scripts/summarize_questionnaire.py
# Summarises the usability/trust questionnaire (docs/evaluation/usability_questionnaire.md).
#
#   cp data/evaluation/questionnaire_responses_template.csv data/evaluation/questionnaire_responses.csv
#   (fill in one row per participant)
#   python -m scripts.summarize_questionnaire [--csv path]

import argparse
from datetime import datetime

import pandas as pd

from app.config import BASE_DIR, DATA_DIR

DEFAULT_CSV = DATA_DIR / "evaluation" / "questionnaire_responses.csv"
SUS_ITEMS = [f"SUS{i}" for i in range(1, 11)]
TRUST_ITEMS = {
    "T1": "Advice was useful for my situation",
    "T2": "Trusted the numbers",
    "T3": "Seeing the reason increased trust",
    "T4": "Seeing the source increased trust",
    "T5": "'Cannot answer' increased trust",
    "T6": "Plan respected essential expenses",
    "T7": "Comfortable in my language",
}
OPEN_ITEMS = ["O1", "O2", "O3", "O4"]


def sus_score(row):
    """Brooke (1996): odd items (x - 1), even items (5 - x), sum x 2.5 -> 0..100."""
    total = 0
    for i, item in enumerate(SUS_ITEMS, start=1):
        x = float(row[item])
        total += (x - 1) if i % 2 else (5 - x)
    return total * 2.5


def summarise(df):
    answered = df.dropna(subset=SUS_ITEMS)
    sus = answered.apply(sus_score, axis=1) if len(answered) else pd.Series(dtype=float)
    trust = {k: (round(df[k].astype(float).mean(), 2), round(df[k].astype(float).std(), 2))
             for k in TRUST_ITEMS if k in df and df[k].notna().any()}
    by_group = {}
    if "knows_budgeting" in df and len(answered):
        for group, part in answered.groupby("knows_budgeting"):
            by_group[group] = round(part.apply(sus_score, axis=1).mean(), 1)
    return {
        "participants": int(len(df)),
        "sus_mean": round(float(sus.mean()), 1) if len(sus) else None,
        "sus_min": round(float(sus.min()), 1) if len(sus) else None,
        "sus_max": round(float(sus.max()), 1) if len(sus) else None,
        "sus_by_budgeting_knowledge": by_group,
        "trust": trust,
    }


def write_report(df, summary):
    out = BASE_DIR / "reports"
    out.mkdir(exist_ok=True)
    path = out / f"questionnaire-{datetime.now():%Y%m%d-%H%M}.md"
    lines = [f"# Usability and trust questionnaire — {datetime.now():%Y-%m-%d}", "",
             f"Participants: {summary['participants']}", "",
             f"**SUS** mean {summary['sus_mean']} (range {summary['sus_min']}–{summary['sus_max']}; "
             "68 is the usual average benchmark)."]
    if summary["sus_by_budgeting_knowledge"]:
        lines.append("By budgeting knowledge: " + ", ".join(
            f"{k}: {v}" for k, v in summary["sus_by_budgeting_knowledge"].items()))
    lines += ["", "| Item | Mean (1–5) | SD |", "|---|---|---|"]
    lines += [f"| {TRUST_ITEMS[k]} | {m} | {sd} |" for k, (m, sd) in summary["trust"].items()]
    for item in OPEN_ITEMS:
        if item in df and df[item].notna().any():
            lines += ["", f"### {item}", ""]
            lines += [f"- {p}: {a}" for p, a in zip(df["participant"], df[item]) if pd.notna(a)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarise questionnaire responses")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    args = parser.parse_args()
    df = pd.read_csv(args.csv)
    summary = summarise(df)
    print(summary)
    print(f"Report: {write_report(df, summary)}")
