from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr

from core import settings
from servicemind import api
from servicemind.evaluation.security_grader import EvidenceOutcome
from servicemind.harness.webhooks import WebhookValidationError, verify_glpi_signature
from servicemind.interfaces.http import operations
from servicemind.interfaces.http.webhook_limits import WebhookAdmission
from servicemind.persistence.models import RunStatus
from servicemind.persistence.repository import RunCancelled, ServiceMindRepository
from servicemind.security.auth import OIDCVerifier, TenantContext, get_tenant_context
from servicemind.security.crypto import CredentialCipher
from servicemind.security.run_access import require_run_access

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")


def principal(**changes):
    return TenantContext.model_validate(
        dict(
            tenant_id=TENANT,
            user_id="owner",
            username="owner",
            roles={"viewer"},
            allowed_glpi_entity_ids={1},
            allowed_glpi_group_ids={3},
        )
        | changes
    )


def run_record(**changes):
    now = datetime.now(UTC)
    return SimpleNamespace(
        **(
            dict(
                id=uuid4(),
                tenant_id=TENANT,
                user_id="owner",
                ticket_id=42,
                goal="security fixture",
                request_write=False,
                status="succeeded",
                result={"canary": "SYNTHETIC_ALLOWED"},
                error=None,
                access_scope={"entity_ids": [1], "group_ids": [3]},
                created_at=now,
                updated_at=now,
            )
            | changes
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(user_id="other"),
        dict(tenant_id=OTHER),
        dict(allowed_glpi_group_ids={4}),
        dict(allowed_glpi_entity_ids={2}),
    ],
)
def test_run_read_scope_rejects_cross_subject_and_revoked_grants(changes):
    with pytest.raises(HTTPException) as exc:
        require_run_access(run_record(), principal(**changes))
    assert exc.value.status_code == 404


def test_run_scope_allows_owner_and_scoped_approver_but_not_unknown_legacy_scope():
    require_run_access(run_record(), principal())
    require_run_access(run_record(), principal(user_id="reviewer", roles={"viewer", "approver"}))
    with pytest.raises(HTTPException):
        require_run_access(run_record(access_scope=None), principal())
    require_run_access(run_record(access_scope=None), principal(roles={"operator"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/events", "/timeline"])
async def test_all_run_views_enforce_role_before_repository(monkeypatch, suffix):
    def forbidden(*args):
        pytest.fail("unauthorized request reached persistence")

    monkeypatch.setattr(api, "ServiceMindRepository", forbidden)
    monkeypatch.setattr(operations, "ServiceMindRepository", forbidden)
    app = FastAPI()
    app.include_router(api.phase2_router)
    app.dependency_overrides[get_tenant_context] = lambda: principal(roles=set())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/servicemind/runs/{uuid4()}{suffix}")
    assert response.status_code == 403


@pytest.mark.parametrize("timestamp", ["", "9" * 5000, "1" * 13, "١٢", "-1", "1e9"])
def test_timestamp_is_bounded_before_integer_conversion(timestamp):
    with pytest.raises(WebhookValidationError):
        verify_glpi_signature(
            body=b"{}", timestamp=timestamp, signature="0" * 64, secret="fixture", now=1000
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timestamp", "body", "expected"), [("9" * 5000, b"{}", 401), ("1000", b"x" * 65537, 413)]
)
async def test_webhook_limits_apply_before_database_or_crypto(
    monkeypatch, timestamp, body, expected
):
    monkeypatch.setattr(api, "ServiceMindRepository", lambda *args: pytest.fail("limit bypassed"))
    app = FastAPI()
    app.include_router(api.phase2_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/webhooks/glpi",
            content=body,
            headers={"X-GLPI-timestamp": timestamp, "X-GLPI-signature": "0" * 64},
        )
    assert response.status_code == expected


@pytest.mark.asyncio
async def test_generic_routes_are_closed_even_with_shared_secret(monkeypatch):
    from service.service import create_app

    monkeypatch.setattr(settings, "SERVICEMIND_GENERIC_API_ENABLED", False)
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("test-secret"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app()), base_url="http://test"
    ) as client:
        for path, body in [
            ("/invoke", {"message": "test"}),
            ("/history", {"thread_id": "other-thread"}),
        ]:
            response = await client.post(
                path, json=body, headers={"Authorization": "Bearer test-secret"}
            )
            assert response.status_code == 404
        assert (await client.get("/threads", params={"user_id": "other"})).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_claim", ["issuer", "audience", "expired", "missing_tenant", "algorithm", None]
)
async def test_real_signed_oidc_tokens_are_validated(monkeypatch, bad_claim):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk["kid"] = "fixture"
    verifier = OIDCVerifier()
    monkeypatch.setattr(
        verifier,
        "_configuration",
        lambda: ("https://issuer.test", "https://issuer.test/jwks", "service"),
    )

    async def jwks(**kwargs):
        return {"keys": [jwk]}

    monkeypatch.setattr(verifier, "_load_jwks", jwks)
    now = datetime.now(UTC)
    claims = {
        "iss": "https://issuer.test",
        "aud": "service",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "sub": "owner",
        "tenant_id": str(TENANT),
        "realm_access": {"roles": ["viewer"]},
        "glpi_entity_ids": [1],
        "glpi_group_ids": [3],
    }
    if bad_claim == "issuer":
        claims["iss"] = "https://other.test"
    if bad_claim == "audience":
        claims["aud"] = "another-resource"
    if bad_claim == "expired":
        claims["exp"] = now - timedelta(minutes=5)
    if bad_claim == "missing_tenant":
        del claims["tenant_id"]
    token = jwt.encode(
        claims,
        private,
        algorithm="RS512" if bad_claim == "algorithm" else "RS256",
        headers={"kid": "fixture"},
    )
    if bad_claim:
        with pytest.raises((jwt.PyJWTError, ValueError)):
            await verifier.verify(token)
    else:
        actual = await verifier.verify(token)
        assert actual.tenant_id == TENANT and actual.roles == {"viewer"}


def test_corrupt_or_rotated_credential_fails_without_returning_plaintext(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(
        settings, "SERVICEMIND_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode())
    )
    cipher = CredentialCipher()
    value = cipher.encrypt("synthetic-secret")
    assert cipher.decrypt(value) == "synthetic-secret"
    with pytest.raises(RuntimeError, match="Unable to decrypt"):
        cipher.decrypt("corrupt")
    monkeypatch.setattr(
        settings, "SERVICEMIND_CREDENTIAL_KEY", SecretStr(Fernet.generate_key().decode())
    )
    with pytest.raises(RuntimeError, match="Unable to decrypt"):
        CredentialCipher().decrypt(value)


@pytest.fixture
def driver():
    spec = importlib.util.spec_from_file_location(
        "security_driver", Path("scripts/verify_phase7_security.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_structured_grading_never_hides_partial_execution(driver):
    rows = [
        dict(nodeid="test_x", when=phase, outcome="passed")
        for phase in ["setup", "call", "teardown"]
    ]
    assert driver.grade_test_phases(rows, process_ok=True) == EvidenceOutcome.PASSED
    assert driver.grade_test_phases(rows[:-1], process_ok=True) == EvidenceOutcome.MISSING
    assert driver.grade_test_phases(rows, process_ok=False) == EvidenceOutcome.MISSING
    assert (
        driver.grade_test_phases(
            rows + [dict(nodeid="test_y", when="setup", outcome="skipped")], process_ok=True
        )
        == EvidenceOutcome.UNEXERCISED
    )
    assert (
        driver.grade_test_phases(
            rows + [dict(nodeid="test_x", when="teardown", outcome="failed")], process_ok=True
        )
        == EvidenceOutcome.FAILED
    )
    rows[1]["wasxfail"] = "known issue"
    assert driver.grade_test_phases(rows, process_ok=True) == EvidenceOutcome.UNEXERCISED


def test_historical_acceptance_cannot_be_relabelled_as_current(driver):
    verdicts, note = driver.acceptance_verdicts()
    assert verdicts == {}
    assert "different source revision" in note


@pytest.mark.asyncio
async def test_webhook_rate_and_concurrency_limits_release_on_abort(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "servicemind.interfaces.http.webhook_limits.time.monotonic", lambda: clock[0]
    )
    admission = WebhookAdmission(per_minute=2, concurrent=1)
    first = admission()
    await anext(first)
    with pytest.raises(HTTPException) as exc:
        await anext(admission())
    assert exc.value.status_code == 429
    await first.aclose()
    second = admission()
    await anext(second)
    await second.aclose()
    with pytest.raises(HTTPException):
        await anext(admission())
    clock[0] += 61
    third = admission()
    await anext(third)
    await third.aclose()
    assert admission._active == 0


@pytest.mark.parametrize("value", [True, -1, 2**40, 1.5, "1", None, []])
def test_webhook_entity_contract_rejects_coercion_and_overflow(value):
    from servicemind.harness.webhooks import parse_glpi_webhook

    body = json.dumps(
        {"tenant_id": str(TENANT), "item": {"id": 1, "entity": {"id": value}}}
    ).encode()
    with pytest.raises(WebhookValidationError):
        parse_glpi_webhook(body)


@pytest.mark.docker
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_database_run_scope_filters_before_pagination_and_rls():
    from sqlalchemy import text

    from servicemind.persistence.database import tenant_session

    repository = ServiceMindRepository(TENANT)
    owner = f"security-owner-{uuid4()}"
    allowed = await repository.create_run(
        user_id=owner,
        ticket_id=1,
        goal="synthetic allowed",
        request_write=False,
        access_scope={"entity_ids": [1], "group_ids": [3]},
    )
    for scope in [
        None,
        {"entity_ids": [2], "group_ids": [3]},
        {"entity_ids": [1], "group_ids": [4]},
    ]:
        await repository.create_run(
            user_id=owner,
            ticket_id=1,
            goal="SYNTHETIC_RESTRICTED_CANARY",
            request_write=False,
            access_scope=scope,
        )
    rows = await repository.list_runs(
        reader_id=owner, reader_scope={"entity_ids": [1], "group_ids": [3]}, limit=1
    )
    assert [row.id for row in rows] == [allowed.id]
    assert await ServiceMindRepository(OTHER).get_run(allowed.id) is None
    async with tenant_session(OTHER) as session:
        # Independent SQL observer, no repository access filter involved.
        assert (
            await session.execute(
                text("SELECT count(*) FROM agent_runs WHERE id = :id"), {"id": allowed.id}
            )
        ).scalar_one() == 0
        role = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert role == (False, False)


@pytest.mark.docker
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_database_cancel_is_a_barrier_and_cannot_be_resurrected():
    repository = ServiceMindRepository(TENANT)
    run = await repository.create_run(
        user_id="security-cancel", ticket_id=1, goal="synthetic cancellation", request_write=True
    )
    await repository.update_run(run.id, RunStatus.RUNNING)
    submitted, release, cancellation_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    external_state = []

    async def inflight():
        async with repository.run_submission(run.id):
            submitted.set()
            await release.wait()
            external_state.append("pre-cancel-inflight-write")

    async def cancel():
        cancellation_started.set()
        return await repository.cancel_run(run.id)

    async with asyncio.timeout(10):
        writer = asyncio.create_task(inflight())
        await submitted.wait()
        cancellation = asyncio.create_task(cancel())
        await cancellation_started.wait()
        await asyncio.sleep(0.05)
        assert not cancellation.done()
        release.set()
        await writer
        assert (await cancellation).status == "cancelled"
        for status in [RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED]:
            with pytest.raises(RunCancelled):
                await repository.update_run(run.id, status)
        with pytest.raises(RunCancelled):
            async with repository.run_submission(run.id):
                external_state.append("FORBIDDEN_POST_CANCEL_WRITE")
    assert external_state == ["pre-cancel-inflight-write"]
    assert (await repository.get_run(run.id)).status == "cancelled"


@pytest.mark.docker
@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.WAITING_REVIEW],
)
async def test_real_database_cancel_http_materializes_updated_at(status):
    repository = ServiceMindRepository(TENANT)
    run = await repository.create_run(
        user_id="owner",
        ticket_id=1,
        goal="synthetic cancel response regression",
        request_write=True,
        access_scope={"entity_ids": [1], "group_ids": [3]},
    )
    if status != RunStatus.PENDING:
        await repository.update_run(run.id, status)
    app = FastAPI()
    app.include_router(api.phase2_router)
    app.dependency_overrides[get_tenant_context] = lambda: principal(roles={"analyst", "viewer"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://isolated"
    ) as client:
        for _ in range(2):
            response = await client.post(f"/v1/servicemind/runs/{run.id}:cancel")
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "cancelled"
            assert datetime.fromisoformat(body["updated_at"]) >= datetime.fromisoformat(
                body["created_at"]
            )
        readback = await client.get(f"/v1/servicemind/runs/{run.id}")
        assert readback.status_code == 200 and readback.json()["status"] == "cancelled"
    assert (await repository.get_run(run.id)).status == "cancelled"


def test_mutation_grading_rejects_setup_failures_and_skipped_nodes(monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import mutation_harness

    rows = [dict(nodeid="test_x", when=p, outcome="passed") for p in ["setup", "call", "teardown"]]
    assert mutation_harness.mutation_status(0, rows) == "GREEN (BAD - no teeth)"
    rows[1]["outcome"] = "failed"
    assert mutation_harness.mutation_status(1, rows) == "RED (good)"
    rows[0]["outcome"] = "failed"
    assert mutation_harness.mutation_status(1, rows) == "UNRUN (no test ran)"
    rows[0]["outcome"] = "skipped"
    assert mutation_harness.mutation_status(1, rows) == "UNEXERCISED (all skipped)"


@pytest.mark.docker
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_mcp_execution_metadata_obeys_run_scope_and_new_runs_keep_scope(monkeypatch):
    from servicemind.mcp import server
    from servicemind.tool_platform.audit import PostgresToolAuditSink

    definition = SimpleNamespace(name="glpi.read.ticket", timeout_seconds=5)
    gateway = SimpleNamespace(
        audit=PostgresToolAuditSink(),
        registry=SimpleNamespace(
            visible=lambda **kwargs: [definition], resolve=lambda *args: definition
        ),
    )
    monkeypatch.setattr(server, "_gateway", gateway)
    repository = ServiceMindRepository(TENANT)
    run = await repository.create_run(
        user_id="owner",
        ticket_id=1,
        goal="MCP scope probe",
        request_write=False,
        access_scope={"entity_ids": [1], "group_ids": [3]},
    )
    request = server.JsonRpcRequest(
        jsonrpc="2.0",
        id=1,
        method="tools/call",
        params={
            "name": "read.ticket",
            "arguments": {"ticket_id": 1},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": server.MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
                "com.servicemind/execution": {
                    "requestId": str(uuid4()),
                    "runId": str(run.id),
                    "taskId": "probe",
                    "deadline": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                },
            },
        },
    )
    for context in [
        principal(user_id="other"),
        principal(allowed_glpi_group_ids={4}),
        principal(tenant_id=OTHER),
    ]:
        with pytest.raises(ValueError, match="outside"):
            await server._tool_call(request, context, "read.ticket")
    assert (await server._tool_call(request, principal(), "read.ticket")).run_id == run.id
    request.params["_meta"].pop("com.servicemind/execution")
    call = await server._tool_call(request, principal(), "read.ticket")
    stored = await repository.get_run(call.run_id)
    require_run_access(stored, principal())
    assert stored.access_scope == {"entity_ids": [1], "group_ids": [3]}
