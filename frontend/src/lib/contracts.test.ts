import { describe, expect, it } from "vitest";
import { releaseStatusSchema, runDetailSchema, runListSchema } from "@/lib/contracts";

const runRow = (ticket_id: number) => ({
  id: "e7b97d9a-3cb9-4b12-85cf-0249cf5b2bfc", ticket_id, request_write: false,
  goal: "MCP read operation: glpi.get_ticket_context", status: "waiting_review",
  created_at: "2026-09-14T11:03:46.051825Z", updated_at: "2026-09-14T15:18:22.438109Z",
});

describe("API contracts", () => {
  it("keeps an MCP read run, which is not bound to any ticket, in the list", () => {
    const parsed = runListSchema.parse({ items: [runRow(0)], next_after_created_at: null, next_after_run_id: null });
    expect(parsed.items[0].ticket_id).toBe(0);
  });

  it("still rejects a ticket_id the backend cannot produce", () => {
    expect(runListSchema.safeParse({ items: [runRow(-1)] }).success).toBe(false);
  });


  it("rejects an action snapshot without a 64-character hash", () => {
    const result = runDetailSchema.safeParse({
      id: "11111111-1111-4111-8111-111111111111", tenant_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      user_id: "alice", ticket_id: 42, goal: "Investigate", request_write: true, status: "waiting_approval",
      result: null, error: null, created_at: "2026-09-22T00:00:00Z", updated_at: "2026-09-22T00:00:00Z",
      action_intent: { id: "22222222-2222-4222-8222-222222222222", action_type: "append_ticket_followup",
        target_id: 42, arguments: {}, risk_level: "low", requires_approval: true, action_hash: "short",
        status: "proposed", intent_version: "v2", policy_version: null, review_digest: null,
        evidence_digest: null, evidence_refs: [], expires_at: null, dry_run_preview: null },
    });
    expect(result.success).toBe(false);
  });

  it("keeps an explicit quality exception distinct from PASS", () => {
    const result = releaseStatusSchema.parse({ generated_at: "2026-09-22T00:00:00Z",
      release_decision: "QUALITY_EXCEPTION_ACCEPTED", structure: { status: "PASS", checks_passed: 14, checks_total: 14 },
      phase5: { status: "PASS_ENGINEERING", tests_passed: 576 }, phase6: { status: "PASS_ENGINEERING", tests_passed: 422 },
      rag: { status: "DOMAIN_QUALITY_NOT_CERTIFIED", recall_at_5: .68, recall_at_10: .76, mrr_at_10: .58,
        ndcg_at_10: .62, scope: "external silver only" }, runtime: { status: "PASS", health_ok: true }, caveats: ["human qrels unavailable"] });
    expect(result.release_decision).toBe("QUALITY_EXCEPTION_ACCEPTED");
    expect(result.rag.status).not.toBe("PASS");
  });
});
