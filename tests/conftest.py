import pytest
from app import config


@pytest.fixture(scope="session", autouse=True)
def test_database(tmp_path_factory):
    original = config.DB_PATH
    config.DB_PATH = tmp_path_factory.mktemp("db") / "test_finance.db"

    from app.database.schema import create_tables

    create_tables()

    yield config.DB_PATH

    config.DB_PATH = original


@pytest.fixture
def user():
    from app.database.crud import add_user

    return add_user("Test User")


@pytest.fixture(scope="session")
def sample_user(test_database):
    from scripts.seed_sample import seed

    return seed()