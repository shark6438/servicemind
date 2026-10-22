"""A local, deterministic model substitute for control integration tests, not model ASR."""

import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
LOG = Path(__file__).with_name("simulated-model-calls.jsonl")
MODEL = "security-simulator-v1"


def walk(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, str):
        try:
            yield from walk(json.loads(value), depth + 1)
        except (ValueError, TypeError):
            return
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item, depth + 1)


def evidence_rows(data):
    result = {}
    for item in walk(data):
        if item.get("source") == "evidence" and str(item.get("item_id", "")).startswith("ev-"):
            result[item["item_id"]] = {
                "evidence_id": item["item_id"],
                **json.loads(item["content"]),
            }
        if item.get("evidence_id") and "content" in item:
            result[item["evidence_id"]] = item
    return list(result.values())


def answer(name, data):
    objects = list(walk(data))
    if name == "SupervisorDecision":
        state = next(x for x in objects if "legal_actions" in x)
        options = state["decision_options"]
        order = [
            "plan",
            "dispatch",
            "join_evidence",
            "analyze",
            "review",
            "handoff_action",
            "finalize",
            "retrieve_more",
            "replan",
            "escalate",
        ]
        choice = min(
            options, key=lambda x: (order.index(x["action"]), -len(x["selected_task_ids"]))
        )
        return {
            **choice,
            "rationale_summary": "Deterministic assessment fixture: advance one legal control transition.",
            "confidence": 1.0,
        }
    if name in {"PlanProposal", "PlanRevisionProposal"}:
        state = next(x for x in objects if "capabilities" in x)
        old = state.get("previous_plan", {}).get("tasks", [])
        completed = state.get("completed_tasks_must_be_preserved", [])
        preserved = [x["task_id"] for x in completed]
        start = max([int(x["task_id"][1:]) for x in old] or [0]) + 1
        ids = [f"T{x}" for x in range(start, start + 5)]
        tasks = [
            {
                "task_id": x["task_id"],
                "agent": x["agent"],
                "task_type": x["task_type"],
                "objective": x.get("input", {}).get("objective", "Preserved completed task"),
                "depends_on": x["depends_on"],
            }
            for x in completed
        ]
        tasks += [
            {
                "task_id": ids[0],
                "agent": "data",
                "task_type": "get_ticket",
                "objective": "Read the selected ticket, support groups and recorded followups.",
                "depends_on": [],
            },
            {
                "task_id": ids[1],
                "agent": "knowledge",
                "task_type": "retrieve_knowledge",
                "objective": "Retrieve authorized VPN multi-factor authentication runbooks.",
                "depends_on": [],
            },
            {
                "task_id": ids[2],
                "agent": "analysis",
                "task_type": "analyze_ticket",
                "objective": "Summarize only facts explicitly recorded in the selected ticket.",
                "depends_on": ids[:2],
            },
            {
                "task_id": ids[3],
                "agent": "reviewer",
                "task_type": "review_analysis",
                "objective": "Independently check evidence and proposed action consistency.",
                "depends_on": [ids[2]],
            },
        ]
        if state.get("request_write"):
            tasks.append(
                {
                    "task_id": ids[4],
                    "agent": "action",
                    "task_type": "propose_followup",
                    "objective": "Propose one reviewed followup, requiring human approval.",
                    "depends_on": [ids[3]],
                }
            )
        result = {
            "rationale_summary": "A fixed, bounded assessment pipeline; service policy still compiles and validates it.",
            "tasks": tasks,
            "max_parallel": 2,
        }
        if name == "PlanRevisionProposal":
            result["preserved_task_ids"] = preserved
        return result
    if name == "DataAcquisitionPlan":
        state = next(x for x in objects if "ticket_id" in x)
        return {
            "calls": [
                {
                    "tool_name": name,
                    "ticket_id": state["ticket_id"],
                    "purpose": "Read authorized fixture evidence",
                }
                for name in ["glpi.read.ticket", "glpi.read.groups", "glpi.read.ticket_followups"]
            ],
            "rationale_summary": "Deterministic read-only acquisition.",
        }
    if name == "QueryProposal":
        return {
            "normalized_query": "VPN MFA handset replacement authentication device rebind",
            "rewritten_queries": [],
            "entities": ["VPN", "MFA"],
            "intent": "procedure",
            "language": "en",
        }
    if name == "AnalysisResult":
        rows = evidence_rows(data)
        if not rows:
            raise ValueError("No evidence delivered to the simulated model")
        ticket = next((r for r in rows if r.get("resource_type") == "ticket"), rows[0])
        try:
            facts = json.loads(ticket["content"])
        except ValueError:
            facts = {}
        if not isinstance(facts, dict):
            facts = {}
        ticket_id = (
            facts.get("id")
            or ticket.get("resource_id")
            or next((x["ticket_id"] for x in objects if "ticket_id" in x), None)
        )
        if ticket_id is None:
            raise ValueError("No selected ticket in the governed evidence")
        refs = [r["evidence_id"] for r in rows]
        groups = [r for r in rows if r.get("resource_type") == "support_group"]
        group = str(groups[0]["content"]).split(":", 1)[-1].strip() if groups else "Service Desk"
        quote = ticket["content"][:700]
        summary = "Recorded ticket evidence: " + quote
        writing = bool(next((x["request_write"] for x in objects if "request_write" in x), False))
        return {
            "classification": "Recorded incident evidence",
            "impact": facts.get("impact", 3),
            "urgency": facts.get("urgency", 3),
            "priority": facts.get("priority", 3),
            "recommended_group": group[:200],
            "recurring_incident": False,
            "problem_recommendation": "Further root-cause investigation requires additional verified evidence.",
            "change_recommendation": "No production configuration change is proposed.",
            "proposed_actions": [
                {
                    "operation": "append_ticket_followup",
                    "resource_type": "ticket",
                    "resource_id": str(ticket_id),
                    "arguments": {"content": summary, "is_private": True},
                    "evidence_refs": [ticket["evidence_id"]],
                    "risk_level": "low",
                }
            ]
            if writing
            else [],
            "reasoning_summary": summary,
            "evidence_refs": refs,
            "confidence": 0.9,
            "source": MODEL,
            "status": "model",
            "claims": [
                {
                    "claim_id": "C1",
                    "claim_type": "incident_fact",
                    "statement": quote,
                    "evidence_refs": [ticket["evidence_id"]],
                    "confidence": 1.0,
                    "assumptions": [],
                }
            ],
            "alternatives": [],
            "unresolved_questions": [
                "This fixed fixture does not infer a root cause or certify business reasoning."
            ],
            "validation_feedback": [],
        }
    if name == "SemanticReview":
        analysis = next(x for x in objects if "claims" in x and "classification" in x)
        rows = evidence_rows(data)
        by_id = {x["evidence_id"]: x for x in rows}
        unsupported = [
            c["claim_id"]
            for c in analysis["claims"]
            if not c["evidence_refs"]
            or any(ref not in by_id for ref in c["evidence_refs"])
            or not any(
                c["statement"] in by_id[ref]["content"]
                for ref in c["evidence_refs"]
                if ref in by_id
            )
        ]
        resource_ids = {
            str(row.get("resource_id")) for row in rows if row.get("resource_type") == "ticket"
        }
        target_ok = all(
            action["resource_type"] == "ticket" and action["resource_id"] in resource_ids
            for action in analysis["proposed_actions"]
        )
        return {
            "claims_supported": not unsupported,
            "action_consistent": target_ok,
            "prompt_injection_detected": False,
            "contradictions": [],
            "unsupported_claim_ids": unsupported,
            "action_target_grounded": target_ok,
            "citation_integrity_ok": True,
            "unbacked_assertions": [],
            "feedback": "Simulated independent check: quoted claims resolve to delivered evidence; the action targets that evidence. Production deterministic citation, approval and authorization controls remain active.",
            "confidence": 1.0,
        }
    raise ValueError(f"Unsupported simulated schema: {name}")


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    if body.get("model") != MODEL:
        raise ValueError("Only the declared assessment simulator is available")
    tool = (body.get("tools") or [{}])[0].get("function")
    schema = body.get("response_format", {}).get("json_schema", {})
    name = tool["name"] if tool else schema.get("name")
    data = []
    for message in body["messages"]:
        if message["role"] in {"user", "human"}:
            try:
                data.append(json.loads(message["content"]))
            except (ValueError, TypeError):
                pass
    result = answer(name, data)
    output = json.dumps(result, ensure_ascii=False)
    with LOG.open("a") as log:
        log.write(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "schema": name,
                    "input_sha256": hashlib.sha256(
                        json.dumps(body, sort_keys=True).encode()
                    ).hexdigest(),
                    "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                    "model": MODEL,
                    "api_fee": 0,
                }
            )
            + "\n"
        )
    identifier = "chatcmpl-" + uuid4().hex
    message = {"role": "assistant", "content": output}
    if tool:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_" + uuid4().hex,
                    "type": "function",
                    "function": {"name": name, "arguments": output},
                }
            ],
        }
    if body.get("stream"):

        async def stream():
            delta = {k: v for k, v in message.items() if v is not None}
            chunk = {
                "id": identifier,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            yield "data: " + json.dumps(chunk) + "\n\n"
            yield (
                "data: "
                + json.dumps(
                    {
                        **chunk,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls" if tool else "stop",
                            }
                        ],
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")
    return {
        "id": identifier,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
