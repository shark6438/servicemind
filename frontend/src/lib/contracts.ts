import { z } from "zod";

export const runStatusSchema = z.enum([
  "pending",
  "running",
  "waiting_approval",
  "waiting_review",
  "succeeded",
  "failed",
  "cancelled",
]);
export type RunStatus = z.infer<typeof runStatusSchema>;

export const runSummarySchema = z.object({
  id: z.string().uuid(),
  // Not positive(): MCP read operations are deliberately not bound to a ticket and
  // persist ticket_id 0. Requiring > 0 rejected those rows and failed the whole list.
  ticket_id: z.number().int().nonnegative(),
  goal: z.string(),
  request_write: z.boolean(),
  status: runStatusSchema,
  created_at: z.string(),
  updated_at: z.string(),
});
export const runListSchema = z.object({
  items: z.array(runSummarySchema),
  next_after_created_at: z.string().nullable().optional(),
  next_after_run_id: z.string().uuid().nullable().optional(),
});
export const actionIntentSchema = z.object({
  id: z.string().uuid(), action_type: z.string(), target_id: z.number().int(),
  arguments: z.record(z.string(), z.unknown()), risk_level: z.string(),
  requires_approval: z.boolean(), action_hash: z.string().length(64), status: z.string(),
  intent_version: z.string(), policy_version: z.string().nullable(),
  review_digest: z.string().nullable(), evidence_digest: z.string().nullable(),
  evidence_refs: z.array(z.string()), expires_at: z.string().nullable(),
  dry_run_preview: z.string().nullable(),
}).passthrough();
export const runDetailSchema = runSummarySchema.extend({
  tenant_id: z.string().uuid(), user_id: z.string(),
  result: z.record(z.string(), z.unknown()).nullable(), error: z.string().nullable(),
  action_intent: actionIntentSchema.nullable(),
});
export const runEventSchema = z.object({
  sequence: z.number().int().positive(), type: z.string(),
  payload: z.record(z.string(), z.unknown()), created_at: z.string(),
});
export const runTimelineSchema = z.array(runEventSchema);
export const auditEventSchema = z.object({
  id: z.string().uuid(), run_id: z.string().uuid().nullable(), actor_id: z.string(),
  event_type: z.string(), resource_type: z.string(), resource_id: z.string(),
  payload: z.record(z.string(), z.unknown()), created_at: z.string(),
});
export const auditPageSchema = z.object({
  items: z.array(auditEventSchema), next_after_created_at: z.string().nullable().optional(),
  next_after_event_id: z.string().uuid().nullable().optional(),
});
const evidenceRefSchema = z.object({
  source_type: z.string().optional(), source_id: z.string().optional(), citation: z.string().optional(),
}).passthrough();
export const memoryReviewItemSchema = z.object({
  memory_id: z.string().uuid(), memory_type: z.enum(["working", "episodic", "semantic", "procedural"]),
  subject_key: z.string(), content: z.string(), content_hash: z.string().length(64),
  version: z.number().int().positive(), status: z.string(), confidence: z.number(),
  importance: z.number(), evidence_refs: z.array(evidenceRefSchema),
  supporting_episode_ids: z.array(z.string().uuid()),
  provenance: z.record(z.string(), z.unknown()), created_at: z.string(),
});
export const memoryQueueSchema = z.object({
  items: z.array(memoryReviewItemSchema), next_after_created_at: z.string().nullable().optional(),
  next_after_memory_id: z.string().uuid().nullable().optional(),
});
export const glpiHealthSchema = z.object({
  status: z.string(), api_version: z.string(), authenticated: z.boolean(),
  entity_id: z.number().int(), profile_id: z.number().int(),
});
export const releaseStatusSchema = z.object({
  generated_at: z.string(), release_decision: z.string(),
  structure: z.object({ status: z.string(), checks_passed: z.number().int(), checks_total: z.number().int() }),
  phase5: z.object({ status: z.string(), tests_passed: z.number().int().nullable() }),
  phase6: z.object({ status: z.string(), tests_passed: z.number().int().nullable() }),
  rag: z.object({ status: z.string(), recall_at_5: z.number().nullable(), recall_at_10: z.number().nullable(),
    mrr_at_10: z.number().nullable(), ndcg_at_10: z.number().nullable(), scope: z.string() }),
  runtime: z.object({ status: z.string(), health_ok: z.boolean().nullable() }),
  caveats: z.array(z.string()),
});

export type RunSummary = z.infer<typeof runSummarySchema>;
export type RunDetail = z.infer<typeof runDetailSchema>;
export type RunEvent = z.infer<typeof runEventSchema>;
export type AuditEvent = z.infer<typeof auditEventSchema>;
export type MemoryReviewItem = z.infer<typeof memoryReviewItemSchema>;
export type ReleaseStatus = z.infer<typeof releaseStatusSchema>;
