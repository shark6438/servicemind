import os
from unittest.mock import patch

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False, help="run docker integration tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: mark test as requiring docker containers")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-docker"):
        skip_docker = pytest.mark.skip(reason="need --run-docker option to run")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip_docker)


@pytest.fixture
def mock_env():
    """Fixture to ensure environment is clean for each test."""
    # Keep the platform-level variables that pathlib uses to resolve the home
    # directory. Clearing USERPROFILE/HOMEDRIVE/HOMEPATH makes Path.home()
    # fail on Windows, which prevents Streamlit's AppTest runner from starting.
    platform_env = {
        key: os.environ[key]
        for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
        if key in os.environ
    }
    with patch.dict(os.environ, platform_env, clear=True):
        yield
