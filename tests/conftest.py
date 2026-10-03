import os
from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_asyncio


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False, help="run docker integration tests"
    )


#: The services a docker test may declare it needs. One marker each, closed on purpose:
#: the point of declaring one is that the roster of live tests can be read and selected by
#: service with a bare expression, ``-m "docker and postgres"``, and a free-text field
#: would give every author a private spelling of the same thing.
DOCKER_REQUIREMENTS = ("postgres", "neo4j", "opensearch", "redis", "deployed_stack")


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: mark test as requiring docker containers")
    for service in DOCKER_REQUIREMENTS:
        config.addinivalue_line("markers", f"{service}: a docker test that needs a live {service}")


def pytest_collection_modifyitems(config, items):
    """Require every docker test to declare what it needs, then gate the whole set.

    The suite used to answer "which live services does this repository's test suite
    exercise?" with a grep, and grep does not notice a new live test arriving without its
    container, or two tests disagreeing about what the same service is called. Declaring
    the requirement turns that into a fact the run enforces, and lets a machine select a
    subset it can actually host: ``pytest -m "docker and postgres" --run-docker``.

    The check runs whether or not ``--run-docker`` was passed, so an undeclared marker is
    rejected on the pull request that adds it rather than on the machine that tries to run
    it.
    """
    for item in items:
        if item.get_closest_marker("docker") is None:
            continue
        declared = [service for service in DOCKER_REQUIREMENTS if list(item.iter_markers(service))]
        if len(declared) != 1:
            raise pytest.UsageError(
                f"{item.nodeid} requires docker containers but does not say which ones. "
                f"Mark it @pytest.mark.docker plus exactly one of "
                f"{['pytest.mark.' + name for name in DOCKER_REQUIREMENTS]}; "
                f"found {declared or 'nothing'}."
            )

    if not config.getoption("--run-docker"):
        skip_docker = pytest.mark.skip(reason="need --run-docker option to run")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip_docker)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Turn a docker test's skip into a failure, but only when docker tests were asked for.

    ``--run-docker`` means "the stack is up, run the tests that need it". Several of those
    tests then decide for themselves whether the stack is really there, and skip when it
    is not: ``test_phase4_graphrag.py`` skips without NEO4J_PASSWORD,
    ``test_phase4_index_lifecycle_live.py`` skips without
    SERVICEMIND_OPENSEARCH_PASSWORD. So ``pytest --run-docker`` could come back all green
    having connected to nothing, and the operator reads that as "the live paths pass".

    Only the combination is a lie. Without ``--run-docker`` the skip is the intended
    behaviour and is left alone; with it, the run claimed a capability it did not have, so
    the skip is reported as a failure naming the missing variable. The escape is to supply
    what the test needs, not to silence it.
    """
    report = yield
    if (
        report.skipped
        and item.config.getoption("--run-docker")
        and item.get_closest_marker("docker") is not None
    ):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} skipped while --run-docker was requested, so nothing about its "
            f"live dependency was verified.\nReason given: {reason}\n"
            f"Supply what the test needs, or do not ask for docker tests."
        )
    return report


#: The tenants the live stack is provisioned with, and the ones the PostgreSQL-marked
#: tests write under. Kept here rather than read from the environment so the tests can
#: provision them; the values match ``scripts/seed_phase2.py``.
PLATFORM_TENANTS = (
    ("11111111-1111-4111-8111-111111111111", "acme", "Acme China"),
    ("22222222-2222-4222-8222-222222222222", "globex", "Globex China"),
)


@pytest_asyncio.fixture(autouse=True)
async def _postgres_tenants(request):
    """Give the PostgreSQL-marked tests the tenants their foreign keys demand.

    Migrations create the ``tenants`` table and every key pointing at it, but nothing
    inserts a row into it. ``scripts/seed_phase2.py`` is the only seeder, and it also
    demands GLPI credentials and a credential key, which a CI runner does not have. So a
    database that has only been migrated -- which is exactly what CI has after
    ``alembic upgrade head`` -- has no tenant to attach a memory, a knowledge document or
    an outbox row to.

    Measured on a scratch database with nothing but the migrations applied: 14 of the 15
    ``-m "docker and postgres"`` tests fail, every one of them on
    ``<table>_tenant_id_fkey`` with ``Key is not present in table "tenants"``. The one
    that passes is the one that inserts its own tenant.

    Provisioning the rows here rather than asking the environment for them is what makes
    that selection runnable on a bare database; it changes no assertion, only whether the
    tests have a tenant to write under.
    """
    if request.node.get_closest_marker("postgres") is None:
        return

    # Imported inside the fixture: the persistence package builds its engine from
    # settings at import time, and a run that selects only in-memory tests should not
    # need a database URL to collect.
    from sqlalchemy.dialects.postgresql import insert

    from servicemind.persistence.database import global_session
    from servicemind.persistence.models import Tenant

    async with global_session() as session:
        for raw_id, slug, name in PLATFORM_TENANTS:
            # Conflict-free rather than keyed on ``id`` alone: ``slug`` is unique too, and
            # a stack provisioned from another source would collide on that instead.
            await session.execute(
                insert(Tenant)
                .values(id=UUID(raw_id), slug=slug, name=name)
                .on_conflict_do_nothing()
            )


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
