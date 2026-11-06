# scripts/seed_profiles.py
# Extra demo users so personalisation and fairness can be shown: the same
# question gives different answers for different finances. Profiles A–E are the
# five users of the Upgrade Report's personalisation table; the last profile is a
# NEW user who only completed onboarding (no history yet).
#
#   python -m scripts.seed_profiles        (after scripts.seed_sample)

from datetime import date

from app.database import crud
from app.services.onboarding import create_user, save_profile
from scripts.seed_sample import _month

# Share of monthly spending per category (sums to 1).
SPLIT = {"Food": 0.35, "Bills": 0.20, "Shopping": 0.15, "Transport": 0.10,
         "Entertainment": 0.08, "Utilities": 0.05, "Education": 0.04, "Healthcare": 0.03}
SUBCATEGORY = {"Food": "Restaurant", "Bills": "Rent", "Shopping": "Clothes",
               "Entertainment": "Movie"}
MONTH_FACTORS = {-2: 0.97, -1: 1.00, 0: 1.03}        # gentle month-to-month variation

PROFILES = [
    # name,                      income, monthly spending
    ("A — high income",          12000, 3000),
    ("B — comfortable",           8000, 5000),
    ("C — tight budget",          6000, 5200),
    ("D — very tight budget",     5000, 4700),
    ("E — overspending",          4500, 4600),
]
LAPTOP = ("Laptop", 5000, 4)                          # the report's example goal


def add_history(uid, income, spending, months=(-2, -1, 0)):
    for offset in months:
        month = _month(offset)
        crud.add_income(uid, income, "Salary", f"{month}-01")
        for category, share in SPLIT.items():
            amount = round(spending * share * MONTH_FACTORS[offset], 2)
            crud.add_transaction(uid, f"{category} spending", amount, category,
                                 f"{month}-12", SUBCATEGORY.get(category))


def add_goal_in_months(uid, name, target, months):
    today = date.today().replace(day=1)
    year = today.year + (today.month - 1 + months) // 12
    month = (today.month - 1 + months) % 12 + 1
    crud.add_goal(uid, name, target, 0, date(year, month, 1).isoformat())


def seed_profiles():
    ids = []
    for name, income, spending in PROFILES:
        uid = create_user(name)
        add_history(uid, income, spending)
        add_goal_in_months(uid, *LAPTOP)
        ids.append(uid)

    new_uid = create_user("New user — onboarding only")
    save_profile(new_uid, 7000, {"Food": 2000, "Bills": 1500, "Transport": 500,
                                 "Shopping": 600, "Entertainment": 300},
                 current_savings=1000,
                 goal={"name": "Laptop", "target_amount": 5000, "months": 4})
    ids.append(new_uid)

    print(f"Seeded {len(ids)} demo profiles: user ids {ids}")
    return ids


if __name__ == "__main__":
    seed_profiles()
