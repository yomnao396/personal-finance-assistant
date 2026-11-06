# app/evaluation/scoring.py
# How each benchmark answer is scored (proposal Table 3.2). Kept separate from
# the runner so the rules are unit-tested and can be quoted in the thesis.
#
#   numeric correct  — some number in the answer is within 1% of the known answer
#   refusal          — the answer says it cannot answer / does not know
#   supported        — a factual answer whose retrieved passages include the
#                      expected source AND which states its key fact
#   hallucination    — a factual answer that is not a refusal and not supported
#   cites source     — the answer names the expected source's publisher

import re

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
REL_TOLERANCE = 0.01

REFUSAL_MARKERS = [
    "cannot answer", "can't answer", "can not answer", "unable to answer", "not able to answer",
    "don't have", "do not have", "don't know", "do not know", "no information",
    "not available", "cannot provide", "can't provide", "cannot predict", "can't predict",
    "not possible to predict", "no access", "real-time", "live data", "trusted sources",
    "لا أستطيع", "لا يمكنني", "لا أملك", "لا تتوفر", "لا يتوفر", "لا أعرف", "ليس لدي",
    "لا تتوفر لدي", "غير متوفر", "لا يمكن التنبؤ",
]

PUBLISHER_NAMES = {
    "Consumer Financial Protection Bureau": ["consumer financial protection bureau", "cfpb",
                                             "مكتب الحماية المالية للمستهلك"],
    "Federal Deposit Insurance Corporation": ["federal deposit insurance", "fdic"],
    "International Monetary Fund": ["international monetary fund", "imf", "صندوق النقد الدولي"],
    "Duke University": ["duke"],
}


def numbers_in(text):
    """Every number in the text, as floats (handles 1,800 and Arabic-Indic digits)."""
    text = (text or "").translate(ARABIC_DIGITS)
    out = []
    for raw in NUMBER.findall(text):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return out


def numeric_correct(answer, expected, rel_tolerance=REL_TOLERANCE):
    tolerance = max(abs(expected) * rel_tolerance, 0.5)
    return any(abs(n - expected) <= tolerance for n in numbers_in(answer))


def is_refusal(answer):
    text = (answer or "").lower()
    return any(marker in text for marker in REFUSAL_MARKERS)


def mentions_keyword(answer, keywords):
    text = (answer or "").lower()
    return any(k.lower() in text for k in keywords)


def cites_publisher(answer, source):
    text = (answer or "").lower()
    publisher = (source or {}).get("publisher", "")
    for full_name, aliases in PUBLISHER_NAMES.items():
        if full_name.lower() in publisher.lower():
            return any(a in text for a in aliases)
    return bool(publisher) and publisher.lower() in text


def score(question, run, knowledge_sources):
    """Score one answer. `run` is run_agent_detailed's output; knowledge_sources
    maps entry id -> source (to check the citation)."""
    answer = run.get("answer", "")
    kind = question["type"]
    row = {"refused": is_refusal(answer)}

    if kind == "numeric":
        row["correct"] = numeric_correct(answer, question["expected"])
    elif kind == "fact":
        retrieved = {s["entry_id"] for s in run.get("sources", [])}
        supported = question["entry"] in retrieved and mentions_keyword(answer, question["keywords"])
        row["supported"] = supported
        row["hallucinated"] = not row["refused"] and not supported
        row["cites_source"] = cites_publisher(answer, knowledge_sources.get(question["entry"]))
    elif kind == "unanswerable":
        row["correct_refusal"] = row["refused"]
    return row


def summarise(rows):
    """Aggregate scored rows (one per question per mode) into the Table 3.2 metrics."""
    def share(items, key):
        values = [r[key] for r in items if key in r]
        return round(sum(values) / len(values), 3) if values else None

    out = {}
    for mode in sorted({r["mode"] for r in rows}):
        mine = [r for r in rows if r["mode"] == mode]
        by = lambda t, d=None: [r for r in mine if r["type"] == t and (d is None or r["difficulty"] == d)]  # noqa: E731
        out[mode] = {
            "numeric_accuracy": share(by("numeric"), "correct"),
            "numeric_accuracy_easy": share(by("numeric", "easy"), "correct"),
            "numeric_accuracy_hard": share(by("numeric", "hard"), "correct"),
            "hallucination_rate": share(by("fact"), "hallucinated"),
            "source_use": share(by("fact"), "cites_source"),
            "correct_cannot_answer": share(by("unanswerable"), "correct_refusal"),
            "questions": len(mine),
        }
    return out
