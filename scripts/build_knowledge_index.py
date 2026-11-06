# scripts/build_knowledge_index.py
# Rebuild the RAG vector store after editing data/knowledge/knowledge_base.json.
# (The app also rebuilds it automatically when the file changes.)

from app.rag.knowledge import INDEX_PATH, build_index

if __name__ == "__main__":
    index = build_index()
    entries = {r["entry_id"] for r in index["records"]}
    print(f"Indexed {len(index['records'])} passages from {len(entries)} sources -> {INDEX_PATH}")
