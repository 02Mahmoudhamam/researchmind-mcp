"""Pytest configuration shared by the whole suite.

Establishes a deterministic settings environment *before* any test module is
imported, so a test run never depends on whatever ``.env`` happens to exist on
the developer's machine.

Why this is done at import time rather than in a fixture
--------------------------------------------------------
pytest imports ``conftest.py`` before it collects test modules, and several
modules call ``get_settings()`` at import time — ``backend/api/app.py:7`` and
``backend/security/jwt_handler.py:8`` among them. By the time a fixture ran,
those imports would already have happened against the developer's environment.

Values are set unconditionally rather than with ``setdefault``. That guarantees
the same configuration on every machine and in CI, and it means a real
``ANTHROPIC_API_KEY`` present in the shell cannot leak into a test run.
"""

import os

_TEST_ENV: dict[str, str] = {
    # Required by Settings and has no default; without it nothing imports.
    "ANTHROPIC_API_KEY": "test-anthropic-api-key",
    # Placeholders. Sprint M2/S2.1 makes the application refuse to start on the
    # "changeme" defaults outside development, so tests set explicit values.
    "SECRET_KEY": "test-secret-key",
    "JWT_SECRET": "test-jwt-secret",
    "APP_ENV": "test",
    "DEBUG": "false",
    "LOG_LEVEL": "WARNING",
}

for _key, _value in _TEST_ENV.items():
    os.environ[_key] = _value


import pytest  # noqa: E402  (must follow the environment setup above)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Clear the ``get_settings`` LRU cache around every test.

    ``get_settings`` is ``@lru_cache()``d, so a test that alters configuration
    would otherwise leak that state into every test that follows.
    """
    from backend.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
