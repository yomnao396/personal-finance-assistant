# scripts/evaluate_assistant.py
# Runs the proposal's evaluation plan (Table 3.2) against the real assistant:
#   numeric accuracy        — tools on ("full") vs tools off ("no_tools")
#   hallucination rate      — RAG on ("full") vs RAG off ("no_rag")
#   source use              — RAG on
#   correct "I cannot answer" — RAG on vs off
# Results are reported overall and by difficulty (easy / hard).
#
# Needs Ollama running with the model pulled (ollama serve; ollama pull qwen2.5:3b).
# It uses a throw-away database with the sample user, so your own data is untouched.
#
#   python -m scripts.evaluate_assistant                  # all modes, all questions
#   python -m scripts.evaluate_assistant --modes full no_tools --limit 5

import argparse
import csv
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

from app import config
from app.config import BASE_DIR, DATA_DIR
from app.evaluation.scoring import score, summarise
from app.rag.knowledge import list_entries

QUESTIONS_PATH = DATA_DIR / "evaluation" / "questions.json"
REPORTS_DIR = BASE_DIR / "reports"
MODES = ["full", "no_tools", "no_rag"]


def load_questions(limit=None):
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]
    return questions[:limit] if limit else questions


def fresh_sample_database():
    """Point the app at a temporary database holding only the sample user."""
    config.DB_PATH = Path(tempfile.mkdtemp()) / "evaluation.db"
    from app.database.schema import create_tables
    from scripts.seed_sample import seed

    create_tables()
    return seed()


def run(modes=MODES, limit=None, chat_fn=None, language="en", out_dir=REPORTS_DIR):
    original_db = config.DB_PATH
    try:
        return _run(modes, limit, chat_fn, language, out_dir)
    finally:
        config.DB_PATH = original_db


def _run(modes, limit, chat_fn, language, out_dir):
    from app.llm.agent import run_agent_detailed

    user_id = fresh_sample_database()
    questions = load_questions(limit)
    sources = {e["id"]: e["source"] for e in list_entries()}
    rows = []

    for mode in modes:
        for q in questions:
            started = time.time()
            try:
                result = run_agent_detailed(q["question"], user_id=user_id, language=language,
                                            mode=mode, chat_fn=chat_fn)
                error = None
            except Exception as exc:  # noqa: BLE001 — one failure must not stop the benchmark
                result, error = {"answer": "", "tools_used": [], "sources": []}, str(exc)
            scored = score(q, result, sources)
            rows.append({
                "mode": mode, "id": q["id"], "type": q["type"], "difficulty": q["difficulty"],
                "question": q["question"], "expected": q.get("expected", q.get("entry", "")),
                "answer": result["answer"],
                "tools_used": ",".join(t["name"] for t in result["tools_used"]),
                "sources": ",".join(s["entry_id"] for s in result.get("sources", [])),
                "seconds": round(time.time() - started, 1), "error": error, **scored,
            })
            print(f"[{mode:8}] {q['id']} {_verdict(rows[-1])}")

    summary = summarise(rows)
    paths = write_report(rows, summary, out_dir)
    return summary, paths


def _verdict(row):
    if row.get("error"):
        return "ERROR " + row["error"][:60]
    for key in ("correct", "supported", "correct_refusal"):
        if key in row:
            return f"{key}={row[key]}"
    return ""


def write_report(rows, summary, out_dir=REPORTS_DIR):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    csv_path = out_dir / f"evaluation-{stamp}.csv"
    md_path = out_dir / f"evaluation-{stamp}.md"

    present = {k for r in rows for k in r}
    fields = [k for k in ROW_ORDER if k in present] + sorted(present - set(ROW_ORDER))
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    def pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    metrics = [
        ("Numeric accuracy (all)", "numeric_accuracy"),
        ("Numeric accuracy (easy)", "numeric_accuracy_easy"),
        ("Numeric accuracy (hard)", "numeric_accuracy_hard"),
        ("Hallucination rate (lower is better)", "hallucination_rate"),
        ("Source use", "source_use"),
        ("Correct 'I cannot answer'", "correct_cannot_answer"),
    ]
    modes = list(summary)
    lines = [
        f"# Assistant evaluation — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "Modes: **full** = tools + RAG; **no_tools** = no tools, raw records in the prompt; "
        "**no_rag** = tools but no knowledge base.",
        "",
        "| Metric | " + " | ".join(modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    for label, key in metrics:
        lines.append(f"| {label} | " + " | ".join(pct(summary[m][key]) for m in modes) + " |")
    lines += ["", f"Questions per mode: {summary[modes[0]]['questions'] if modes else 0}. "
              f"Raw answers: `{csv_path.name}`.", "",
              "Scoring rules: app/evaluation/scoring.py."]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": str(csv_path), "markdown": str(md_path)}


ROW_ORDER = ["mode", "id", "type", "difficulty", "question", "expected", "answer", "correct",
             "supported", "hallucinated", "cites_source", "correct_refusal", "refused",
             "tools_used", "sources", "seconds", "error"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the assistant (proposal Table 3.2)")
    parser.add_argument("--modes", nargs="+", default=MODES, choices=MODES)
    parser.add_argument("--limit", type=int, default=None, help="only the first N questions")
    parser.add_argument("--language", default="en", choices=["en", "ar"])
    args = parser.parse_args()

    from app.llm.qwen import status

    llm = status()
    if not llm["available"]:
        raise SystemExit(f"Cannot evaluate: {llm['reason']}")

    summary, paths = run(args.modes, args.limit, language=args.language)
    print(json.dumps(summary, indent=2))
    print(f"\nReport: {paths['markdown']}\nAnswers: {paths['csv']}")
