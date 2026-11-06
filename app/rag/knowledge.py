# app/rag/knowledge.py
# Retrieval-Augmented Generation (RAG) over a small, verified knowledge base.
#
#   data/knowledge/knowledge_base.json  — short passages (English + Arabic), each
#                                         with the source it paraphrases
#   models/knowledge_index.joblib       — the vector store: one TF-IDF vector per
#                                         passage (rebuilt automatically when the
#                                         knowledge base file changes)
#
# Vectors are TF-IDF over character n-grams rather than a neural embedding model:
# no download, works offline, and character n-grams match Arabic word forms
# (الادخار / ادخار) as well as English ones. A question is turned into the same
# kind of vector and the closest passages are returned by cosine similarity.
# Below MIN_SCORE nothing is returned, so the assistant says it cannot answer
# instead of guessing.

import hashlib
import json

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.config import DATA_DIR, MODELS_DIR

KB_PATH = DATA_DIR / "knowledge" / "knowledge_base.json"
INDEX_PATH = MODELS_DIR / "knowledge_index.joblib"
MIN_SCORE = 0.20          # measured: relevant questions >= 0.227, unrelated <= 0.174
DEFAULT_K = 3

_index = None


def _kb_hash():
    return hashlib.sha256(KB_PATH.read_bytes()).hexdigest()


def load_passages():
    """Flatten the knowledge base: one record per passage per language."""
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    records = []
    for entry in kb["entries"]:
        for i, passage in enumerate(entry["passages"]):
            for lang, text in passage.items():
                records.append({
                    "entry_id": entry["id"],
                    "passage_id": f"{entry['id']}#{i + 1}",
                    "lang": lang,
                    "title": entry["title"].get(lang, entry["title"]["en"]),
                    "text": text,
                    "source": entry["source"],
                })
    return records


def build_index():
    """Vectorise every passage and save the vector store to disk."""
    records = load_passages()
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                 sublinear_tf=True, min_df=1)
    matrix = vectorizer.fit_transform(
        [f"{r['title']} {r['text']}" for r in records])
    index = {"kb_hash": _kb_hash(), "records": records,
             "vectorizer": vectorizer, "matrix": matrix}
    joblib.dump(index, INDEX_PATH)
    return index


def load_index(force_rebuild=False):
    global _index
    if _index is not None and not force_rebuild and _index["kb_hash"] == _kb_hash():
        return _index
    index = None
    if INDEX_PATH.exists() and not force_rebuild:
        index = joblib.load(INDEX_PATH)
        if index.get("kb_hash") != _kb_hash():
            index = None
    _index = index or build_index()
    return _index


def search(query, k=DEFAULT_K, lang=None, min_score=MIN_SCORE):
    """Closest passages to the question, best first, at most one per entry.

    Returns {"found": bool, "results": [...]} where each result carries the
    passage text and its source so the answer can cite it.
    """
    if not query or not query.strip():
        return {"found": False, "results": [], "note": "empty question"}

    index = load_index()
    scores = cosine_similarity(index["vectorizer"].transform([query]), index["matrix"])[0]
    ranked = sorted(range(len(scores)), key=lambda i: -scores[i])

    results, seen = [], set()
    for i in ranked:
        record = index["records"][i]
        if scores[i] < min_score:
            break
        if record["entry_id"] in seen:
            continue
        seen.add(record["entry_id"])
        passage = dict(record)
        if lang and record["lang"] != lang:
            passage = _same_passage_in(index, record["passage_id"], lang) or passage
        passage["score"] = round(float(scores[i]), 3)
        results.append(passage)
        if len(results) == k:
            break

    return {
        "found": bool(results),
        "results": results,
        "note": ("answer using only these passages and name their source"
                 if results else
                 "nothing in the verified knowledge base matches — say you cannot answer "
                 "this from trusted sources instead of guessing"),
    }


def _same_passage_in(index, passage_id, lang):
    return next((dict(r) for r in index["records"]
                 if r["passage_id"] == passage_id and r["lang"] == lang), None)


def list_entries():
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    return [{"id": e["id"], "title": e["title"], "source": e["source"],
             "passages": e["passages"]} for e in kb["entries"]]
