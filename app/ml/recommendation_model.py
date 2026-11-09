# app/ml/recommendation_model.py
# Random Forest multi-label recommendation model.
# It predicts applicable recommendation actions from engineered financial features.
#
# WHAT IT DOES:
# Given one user's financial STATE (28 engineered features), it predicts
# WHICH recommendation actions apply. Several can be true at once.
#
# WHAT IT DOES NOT DO:
# It never classifies transactions, never computes a financial gap,
# never decides an amount to cut, and never writes advice.

import joblib
import numpy as np
import pandas as pd

from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    f1_score,
    hamming_loss,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from sklearn.multioutput import MultiOutputClassifier

from app.config import MODELS_DIR
from app.ml.feature_engineering import (
    ACTION_NAMES,
    FEATURE_COLUMNS,
    LABEL_COLUMNS,
    assert_no_leakage,
    features_to_row,
    split_x_y,
)

MODEL_PATH = MODELS_DIR / "recommendation_rf.pkl"
DEFAULT_THRESHOLD = 0.5

_bundle = None


# ---------------------------------------------------------------------------
# training
# ---------------------------------------------------------------------------

def train_recommendation_model(
    csv_path,
    test_size=0.2,
    random_state=42,
    n_estimators=300,
    max_depth=None,
    min_samples_leaf=2,
):
    """Train, evaluate per label, and save the model bundle."""

    assert_no_leakage()

    df = pd.read_csv(csv_path, encoding="cp1252")

    # X and Y are selected by NAME from two disjoint lists.
    X, Y = split_x_y(df)

    # Split before fitting so test rows are never seen.
    X_train, X_test, Y_train, Y_test = train_test_split(
        X,
        Y,
        test_size=test_size,
        random_state=random_state,
    )

    base = RandomForestClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
        class_weight="balanced_subsample",
        random_state=random_state,
        n_jobs=-1,
    )

    model = MultiOutputClassifier(base)

    model.fit(X_train, Y_train)

    predictions = model.predict(X_test)

    per_label = {}

    for i, name in enumerate(LABEL_COLUMNS):
        y_true = Y_test.iloc[:, i]
        y_pred = predictions[:, i]

        per_label[name] = {
            "accuracy": round(
                float(
                    accuracy_score(
                        y_true,
                        y_pred,
                    )
                ),
                4,
            ),
            "precision": round(
                float(
                    precision_score(
                        y_true,
                        y_pred,
                        zero_division=0,
                    )
                ),
                4,
            ),
            "recall": round(
                float(
                    recall_score(
                        y_true,
                        y_pred,
                        zero_division=0,
                    )
                ),
                4,
            ),
            "f1": round(
                float(
                    f1_score(
                        y_true,
                        y_pred,
                        zero_division=0,
                    )
                ),
                4,
            ),
            "positive_rows_in_test": int(
                y_true.sum()
            ),
        }

    exact_match = float(
        (predictions == Y_test.values).all(axis=1).mean()
    )

    metrics = {
        "n_rows": int(len(df)),
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "per_label": per_label,
        "subset_accuracy": round(
            exact_match,
            4,
        ),
        "hamming_loss": round(
            float(
                hamming_loss(
                    Y_test,
                    predictions,
                )
            ),
            4,
        ),
        "macro_f1": round(
            float(
                f1_score(
                    Y_test,
                    predictions,
                    average="macro",
                    zero_division=0,
                )
            ),
            4,
        ),
        "micro_f1": round(
            float(
                f1_score(
                    Y_test,
                    predictions,
                    average="micro",
                    zero_division=0,
                )
            ),
            4,
        ),
    }

    bundle = {
        "model": model,
        "feature_columns": list(FEATURE_COLUMNS),
        "label_columns": list(LABEL_COLUMNS),
        "metrics": metrics,
    }

    MODEL_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    joblib.dump(
        bundle,
        MODEL_PATH,
    )

    global _bundle
    _bundle = bundle

    print(
        f"Trained on {metrics['n_train']} scenarios, "
        f"tested on {metrics['n_test']}."
    )

    print(
        f"{'label':<24}"
        f"{'acc':>8}"
        f"{'prec':>8}"
        f"{'rec':>8}"
        f"{'f1':>8}"
        f"{'pos':>6}"
    )

    for name, m in per_label.items():
        print(
            f"{name:<24}"
            f"{m['accuracy']:>8.3f}"
            f"{m['precision']:>8.3f}"
            f"{m['recall']:>8.3f}"
            f"{m['f1']:>8.3f}"
            f"{m['positive_rows_in_test']:>6}"
        )

    print(
        f"\nsubset (exact-match) accuracy: "
        f"{metrics['subset_accuracy']:.3f}"
    )

    print(
        f"hamming loss: {metrics['hamming_loss']:.4f}   "
        f"macro-F1: {metrics['macro_f1']:.3f}   "
        f"micro-F1: {metrics['micro_f1']:.3f}"
    )

    print(
        f"Saved -> {MODEL_PATH}"
    )

    return metrics


def full_report(
    csv_path,
    test_size=0.2,
    random_state=42,
):
    """Per-label sklearn classification report."""

    df = pd.read_csv(
        csv_path,
        encoding="cp1252",
    )

    X, Y = split_x_y(df)

    _, X_test, _, Y_test = train_test_split(
        X,
        Y,
        test_size=test_size,
        random_state=random_state,
    )

    model = load_model()

    predictions = model.predict(X_test)

    return classification_report(
        Y_test,
        predictions,
        target_names=LABEL_COLUMNS,
        zero_division=0,
    )


# ---------------------------------------------------------------------------
# loading & prediction
# ---------------------------------------------------------------------------

def load_bundle(force_reload=False):
    global _bundle

    if _bundle is None or force_reload:

        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                f"{MODEL_PATH} not found. Run: "
                f"python -m scripts.train_recommendation_model"
            )

        _bundle = joblib.load(
            MODEL_PATH
        )

        if _bundle["feature_columns"] != list(
            FEATURE_COLUMNS
        ):
            raise ValueError(
                "the saved model was trained on different "
                "feature columns — retrain with: "
                "python -m scripts.train_recommendation_model"
            )

    return _bundle


def load_model(force_reload=False):
    return load_bundle(force_reload)["model"]


def model_is_available():
    return MODEL_PATH.exists()


def predict_recommendations(
    features,
    threshold=DEFAULT_THRESHOLD,
):
    """
    features: the dict from feature_engineering.build_features.

    Returns:
        {
            "actions": [...],
            "labels": {...},
            "probabilities": {...}
        }
    """

    model = load_model()

    row = np.array(
        [features_to_row(features)],
        dtype=float,
    )

    frame = pd.DataFrame(
        row,
        columns=FEATURE_COLUMNS,
    )

    probabilities = {}

    for i, name in enumerate(LABEL_COLUMNS):

        estimator = model.estimators_[i]

        classes = list(
            estimator.classes_
        )

        if 1 in classes:
            probabilities[name] = round(
                float(
                    estimator.predict_proba(frame)[0][
                        classes.index(1)
                    ]
                ),
                4,
            )
        else:
            probabilities[name] = 0.0

    labels = {
        name: int(
            probabilities[name] >= threshold
        )
        for name in LABEL_COLUMNS
    }

    if not any(labels.values()):
        # Never return an empty recommendation set.
        # Take the strongest signal.
        best = max(
            probabilities,
            key=probabilities.get,
        )

        labels[best] = 1

    # "Maintain Current Plan" is exclusive by definition.
    if (
        labels["Maintain_Current_Plan"]
        and sum(labels.values()) > 1
    ):
        if probabilities[
            "Maintain_Current_Plan"
        ] >= max(
            probabilities[n]
            for n in LABEL_COLUMNS
            if n != "Maintain_Current_Plan"
        ):
            labels = {
                n: 0
                for n in LABEL_COLUMNS
            }

            labels[
                "Maintain_Current_Plan"
            ] = 1

        else:
            labels[
                "Maintain_Current_Plan"
            ] = 0

    actions = [
        n
        for n in LABEL_COLUMNS
        if labels[n]
    ]

    return {
        "actions": actions,
        "action_names": [
            ACTION_NAMES[a]
            for a in actions
        ],
        "labels": labels,
        "probabilities": probabilities,
        "threshold": threshold,
        "source": "random_forest",
    }


def get_feature_importance(top_n=None):
    """
    Average Gini importance across the per-label forests,
    plus per-label detail.
    """

    model = load_model()

    per_label = {}

    total = np.zeros(
        len(FEATURE_COLUMNS)
    )

    for i, name in enumerate(LABEL_COLUMNS):

        importances = (
            model.estimators_[i]
            .feature_importances_
        )

        total += importances

        per_label[name] = {
            FEATURE_COLUMNS[j]: round(
                float(importances[j]),
                5,
            )
            for j in np.argsort(
                importances
            )[::-1]
        }

    mean = total / len(
        LABEL_COLUMNS
    )

    order = np.argsort(mean)[::-1]

    overall = {
        FEATURE_COLUMNS[j]: round(
            float(mean[j]),
            5,
        )
        for j in order
    }

    if top_n:
        overall = dict(
            list(overall.items())[:top_n]
        )

    return {
        "overall": overall,
        "per_label": per_label,
    }


def get_metrics():
    return load_bundle()["metrics"]