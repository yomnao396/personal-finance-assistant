# scripts/validate_forecast.py
# Validates the spending forecast's METHOD on synthetic users (Intelligence
# Design §N): ~100 users x 12 months, a gentle trend plus realistic noise.
# For each user the forecast sees the first months and predicts the next one.
#
#   MAE / RMSE / MAPE      — how far the forecast (mu) is from what happened
#   naive baseline         — "next month = last month", for comparison
#   calibration            — share of real values inside mu +/- 1 sigma (ideal ~68%)
#                            and mu +/- 2 sigma (ideal ~95%)
#
# Honest limit: synthetic data validates the method, not real-world accuracy.
#
#   python -m scripts.validate_forecast [--users 100 --seed 7]

import argparse
from datetime import datetime

import numpy as np

from app.config import BASE_DIR
from app.ml.prediction import forecast_from_values


def synthetic_users(n_users=100, months=12, seed=7):
    rng = np.random.default_rng(seed)
    users = []
    for _ in range(n_users):
        level = rng.uniform(1500, 9000)
        trend = rng.normal(0, 0.02) * level            # about +/-2% of the level per month
        noise = rng.uniform(0.03, 0.15) * level        # each user has their own volatility
        values = [max(level + trend * t + rng.normal(0, noise), 0) for t in range(months)]
        users.append([round(v, 2) for v in values])
    return users


def evaluate(users, history):
    """Forecast month `history` from the months before it."""
    errors, naive_errors, actuals, in_1, in_2 = [], [], [], 0, 0
    for values in users:
        past, actual = values[:history], values[history]
        f = forecast_from_values(past)
        errors.append(f["mu"] - actual)
        naive_errors.append(past[-1] - actual)
        actuals.append(actual)
        in_1 += abs(actual - f["mu"]) <= f["sigma"]
        in_2 += abs(actual - f["mu"]) <= 2 * f["sigma"]
    errors, naive_errors, actuals = map(np.asarray, (errors, naive_errors, actuals))
    n = len(users)
    return {
        "history_months": history,
        "mae": round(float(np.mean(np.abs(errors))), 1),
        "rmse": round(float(np.sqrt(np.mean(errors ** 2))), 1),
        "mape_pct": round(float(np.mean(np.abs(errors) / actuals) * 100), 2),
        "naive_mae": round(float(np.mean(np.abs(naive_errors))), 1),
        "within_1_sigma_pct": round(in_1 / n * 100, 1),
        "within_2_sigma_pct": round(in_2 / n * 100, 1),
    }


def main(n_users=100, seed=7):
    users = synthetic_users(n_users, seed=seed)
    results = [evaluate(users, h) for h in (1, 2, 3, 6, 11)]

    out_dir = BASE_DIR / "reports"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"forecast-validation-{datetime.now():%Y%m%d-%H%M}.md"
    lines = [
        f"# Spending forecast validation — {datetime.now():%Y-%m-%d %H:%M}", "",
        f"{n_users} synthetic users, 12 months each (random seed {seed}): level 1,500–9,000, "
        "trend about ±2%/month, noise 3–15% per user. Each row forecasts the next month "
        "from the given number of months of history.", "",
        "| History | MAE | RMSE | MAPE | Naive MAE | Within ±1σ (ideal 68%) | Within ±2σ (ideal 95%) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['history_months']} mo | {r['mae']:,} | {r['rmse']:,} | {r['mape_pct']}% | "
                     f"{r['naive_mae']:,} | {r['within_1_sigma_pct']}% | {r['within_2_sigma_pct']}% |")
    lines += ["", "1–2 months use the average with σ = 15%; 3+ months use linear regression "
              "with σ = the regression's prediction standard error, Student-t corrected "
              "(minimum 5%). Synthetic data validates the method, not real-world accuracy."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results, path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate the spending forecast")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    results, path = main(args.users, args.seed)
    for r in results:
        print(r)
    print(f"Report: {path}")
