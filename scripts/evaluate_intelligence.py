# scripts/evaluate_intelligence.py
# Runs the financial-intelligence cases (app/evaluation/intelligence_cases.py)
# through the HTTP API, the same /chat endpoint the Streamlit assistant calls,
# with the local Qwen model behind it, in a throw-away database (the real
# data/finance.db is never opened). Prints and saves, per dimension: intent,
# entity extraction, data retrieval, calculation, final answer, and the share of
# answers with amounts that no record supports.
#
#   python scripts/evaluate_intelligence.py              # with the local model
#   python scripts/evaluate_intelligence.py --no-model   # fail any model call

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-model", action="store_true", help="fail when a question reaches the model")
    args = parser.parse_args()

    config.DB_PATH = Path(tempfile.mkdtemp()) / "evaluation.db"
    from app.database.schema import create_tables
    create_tables()

    from fastapi.testclient import TestClient

    from app.api.main import app
    from app.evaluation.intelligence import record_figures, run
    from app.evaluation.intelligence_cases import CASES, build_profile
    from app.services.intent import language_of

    if args.no_model:
        import app.llm.agent as agent

        def refuse(messages, tools=None):
            raise RuntimeError("model called")
        agent.chat = refuse

    uid = build_profile()
    client = TestClient(app)
    timings = []

    def ask(question, history):
        start = time.perf_counter()
        r = client.post("/chat", json={"user_id": uid, "message": question, "history": history,
                                       "language": language_of(question, "ar")})
        timings.append(time.perf_counter() - start)
        if r.status_code != 200:
            return {"answer": f"HTTP {r.status_code}: {r.text}", "tools_used": [], "context": {}, "tool_results": []}
        return r.json()

    rows, summary = run(CASES, ask, record_figures(uid))
    summary["seconds_per_question"] = round(sum(timings) / len(timings), 3)
    summary["model_answers"] = sum(1 for r in rows if not r["fast_path"])

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out = Path("reports") / f"intelligence-{stamp}.md"
    out.parent.mkdir(exist_ok=True)
    lines = [f"# Financial intelligence evaluation ({stamp})", "",
             "Run through the HTTP /chat endpoint" + (" without the model." if args.no_model else
                                                    " with the local Qwen model behind it."), "",
             "| Dimension | Passed | Checked | Rate |", "|---|---|---|---|"]
    for dim in ("intent", "entities", "retrieval", "calculation", "answer"):
        d = summary[dim]
        lines.append(f"| {dim} | {d['passed']} | {d['checked']} | {d['rate']} |")
    f = summary["fabricated_answers"]
    lines += [f"| answers with unsupported amounts | {f['count']} | {f['of']} | {f['rate']} |", "",
              f"Fully correct: {summary['fully_correct']} of {summary['cases']} cases. "
              f"Answered by the model: {summary['model_answers']}. "
              f"Average time: {summary['seconds_per_question']} s per question.", "", "## Cases", ""]
    for r in rows:
        mark = "✅" if not r["problems"] else "❌"
        lines.append(f"### {mark} {r['id']} ({r['group']})")
        lines.append(f"**Q:** {r['question']}  ")
        lines.append("**A:** " + r["reply"].replace("\n", "  \n"))
        if r["problems"]:
            lines.append("**Problems:** " + "; ".join(r["problems"]))
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"report: {out}")
    return 0 if summary["fully_correct"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
