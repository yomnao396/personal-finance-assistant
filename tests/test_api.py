
# tests/test_api.py

# Verifies the FastAPI layer: the new analysis, recommendation and preference
# endpoints, category validation on input, and that the classifier route is gone.

import pytest
from fastapi.testclient import TestClient

from app.api.main import app


client = TestClient(app)


def test_health():
    """The API starts and answers. Expected: {'status': 'ok'}."""
    assert client.get("/health").json() == {"status": "ok"}


def test_categories_endpoint_serves_the_database_tree():
    """
    GET /categories returns the tree from the DATABASE, not a hard-coded list.

    Expected:
    - Bills has its five subcategories.
    - Transport has no subcategories.
    """
    tree = client.get("/categories").json()["categories"]

    assert tree["Bills"] == [
        "Rent",
        "Electricity",
        "Water",
        "Internet",
        "Other",
    ]

    assert tree["Transport"] == []


def test_add_transaction_with_subcategory(sample_user):
    """
    A transaction posted with a subcategory is accepted and stored.

    Expected:
    - HTTP 200
    - status == 'added'
    """
    r = client.post(
        "/transactions",
        json={
            "user_id": sample_user,
            "description": "Lunch",
            "amount": 50,
            "category": "Food",
            "subcategory": "Lunch",
            "date": "2026-08-20",
        },
    )

    assert r.status_code == 200
    assert r.json()["status"] == "added"

    client.delete(
        f"/transactions/{r.json()['id']}"
    )


def test_add_transaction_rejects_a_bad_combination(sample_user):
    """
    An invalid category/subcategory pair returns 400,
    not a stored row.
    """
    r = client.post(
        "/transactions",
        json={
            "user_id": sample_user,
            "description": "Bad",
            "amount": 50,
            "category": "Food",
            "subcategory": "Movie",
        },
    )

    assert r.status_code == 400
    assert "subcategory" in r.json()["detail"]


def test_add_transaction_rejects_a_negative_amount(sample_user):
    """
    The original amount guard still works.

    Expected:
    - HTTP 400
    """
    r = client.post(
        "/transactions",
        json={
            "user_id": sample_user,
            "description": "Bad",
            "amount": -5,
            "category": "Food",
        },
    )

    assert r.status_code == 400


@pytest.mark.parametrize(
    "path, key",
    [
        ("/analysis/categories", "dominant_subcategory"),
        ("/analysis/income", "primary_source"),
        ("/analysis/patterns", "monthly_spending"),
        ("/analysis/savings-goal", "goal_analysis"),
    ],
)
def test_analysis_endpoints(path, key, sample_user):
    """
    Each analysis endpoint answers with its own analysis block.

    Expected:
    - HTTP 200
    - named key is present
    """
    r = client.get(
        path,
        params={"user_id": sample_user},
    )

    assert r.status_code == 200
    assert key in r.json()


def test_recommendation_plan_endpoint(sample_user):
    """
    The recommendation plan is reachable over HTTP
    and carries the same numbers as the engine.

    Expected:
    - 1200 monthly gap for the seeded user.
    """
    plan = client.get(
        "/recommendations/plan",
        params={"user_id": sample_user},
    ).json()

    assert plan["financial_gap"]["target_amount"] == 1200.0
    assert "reductions" in plan


def test_preferences_crud_over_http(user):
    """
    Create, read, update and delete a preference through the API.
    """
    body = {
        "user_id": user,
        "main_category": "Food",
        "subcategory": "Restaurant",
        "is_essential": True,
    }

    # Create
    assert client.post(
        "/preferences",
        json=body,
    ).status_code == 200

    # Read
    listed = client.get(
        "/preferences",
        params={"user_id": user},
    ).json()["preferences"]

    assert listed[0]["subcategory"] == "Restaurant"
    assert listed[0]["is_essential"] is True

    # Update
    body["is_essential"] = False

    assert client.put(
        "/preferences",
        json=body,
    ).status_code == 200

    # Verify update
    listed = client.get(
        "/preferences",
        params={"user_id": user},
    ).json()["preferences"]

    assert listed[0]["is_essential"] is False

    # Delete
    r = client.delete(
        "/preferences",
        params={
            "user_id": user,
            "main_category": "Food",
            "subcategory": "Restaurant",
        },
    )

    assert r.status_code == 200

    # Verify deletion
    assert client.get(
        "/preferences",
        params={"user_id": user},
    ).json()["preferences"] == []


def test_preference_endpoint_rejects_an_invalid_pair(user):
    """
    Validation is enforced at the API boundary too.

    Expected:
    - HTTP 400 for Transport/Lunch.
    """
    r = client.post(
        "/preferences",
        json={
            "user_id": user,
            "main_category": "Transport",
            "subcategory": "Lunch",
            "is_essential": True,
        },
    )

    assert r.status_code == 400


def test_updating_a_missing_preference_returns_404(user):
    """
    Updating something that does not exist is an error,
    not a silent insert.

    Expected:
    - HTTP 404
    """
    r = client.put(
        "/preferences",
        json={
            "user_id": user,
            "main_category": "Food",
            "subcategory": "Dinner",
            "is_essential": True,
        },
    )

    assert r.status_code == 404


def test_no_classifier_route_remains():
    """
    No classifier endpoint survived the removal.

    Expected:
    - No route path contains 'classify'.
    """
    paths = {
        r.path
        for r in app.routes
        if hasattr(r, "path")
    }

    assert not any(
        "classify" in p
        for p in paths
    )

