# scripts/train_recommendation_model.py
# Generates the scenario dataset if it is missing, then trains and saves the
# Random Forest multi-label recommendation model.

import argparse

from app.ml.recommendation_model import (
    MODEL_PATH,
    get_feature_importance,
    train_recommendation_model,
)

from scripts.generate_recommendation_dataset import OUT_PATH, generate


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Train the recommendation Random Forest"
    )

    parser.add_argument("--csv", default=str(OUT_PATH))
    parser.add_argument("--rows", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="rebuild the scenario dataset before training",
    )

    args = parser.parse_args()

    csv_path = args.csv

    if args.regenerate or not OUT_PATH.exists():
        csv_path, problems = generate(
            n_rows=args.rows,
            seed=args.seed,
        )

        if problems:
            raise SystemExit(
                "Dataset validation failed — fix it before training."
            )

    print()

    train_recommendation_model(csv_path)

    print("\nTop 12 features by importance (averaged over the 8 label forests):")

    for name, value in get_feature_importance(top_n=12)["overall"].items():
        print(f"  {name:<28} {value:.4f}")

    print(f"\nModel file: {MODEL_PATH}")