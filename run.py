# run.py
# Convenience script: create tables, seed the category reference data and the
# sample user, and make sure the recommendation model exists.
from app.database.schema import create_tables
from app.ml.recommendation_model import MODEL_PATH
from scripts.seed_sample import seed
 
if __name__ == "__main__":
    create_tables()
    seed()
    if not MODEL_PATH.exists():
        print("\nNo recommendation model yet. Train it with:")
        print("   python -m scripts.train_recommendation_model --regenerate")
    else:
        print(f"\nRecommendation model found: {MODEL_PATH.name}")
