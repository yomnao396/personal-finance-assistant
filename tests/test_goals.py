# tests/test_goals.py
from app.engines.goals_engine import calculate_goal_progress, calculate_goal_time


GOAL = {
    "target_amount": 5000,
    "current_amount": 3000
}


def test_progress():
    p = calculate_goal_progress(GOAL)

    assert p["remaining"] == 2000
    assert p["percent_complete"] == 60.0


def test_time():
    result = calculate_goal_time(GOAL, 1000)

    assert result["months_needed"] == 2


def test_zero_saving_guard():
    result = calculate_goal_time(GOAL, 0)

    assert "error" in result