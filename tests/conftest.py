import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from tests.helpers import ADMIN_EMAIL, auth_headers


@pytest.fixture
def admin_headers():
    return auth_headers(ADMIN_EMAIL, "Admin")


@pytest.fixture
def auth_headers_for():
    def _build(email: str, tipo: str = "Usuário"):
        return auth_headers(email, tipo)

    return _build


@pytest.fixture
def cleanup_stack():
    callbacks = []
    yield callbacks
    failures = []
    for callback in reversed(callbacks):
        try:
            callback()
        except Exception as exc:  # pragma: no cover - cleanup diagnostics only
            failures.append(str(exc))
    assert not failures, "cleanup failures: " + " | ".join(failures)