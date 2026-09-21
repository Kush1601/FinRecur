import { z } from "zod";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Result<T> = { ok: true; data: T } | { ok: false; error: string; status?: number };

async function request<T>(path: string, schema: z.ZodType<T>, init?: RequestInit): Promise<Result<T>> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
      cache: "no-store",
    });
  } catch {
    return { ok: false, error: "network_error" };
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    return { ok: false, error: typeof detail === "string" ? detail : `Request failed (${res.status})`, status: res.status };
  }
  let json: unknown;
  try {
    json = await res.json();
  } catch {
    return { ok: false, error: "invalid_json" };
  }
  const parsed = schema.safeParse(json);
  if (!parsed.success) {
    return { ok: false, error: `schema_mismatch: ${parsed.error.message}` };
  }
  return { ok: true, data: parsed.data };
}

// ---- Runs ----

export const RunCountsSchema = z.object({
  matched: z.number().optional(),
  escalated: z.number().optional(),
  applied_centavos: z.number().optional(),
  written_off_centavos: z.number().optional(),
  total_receipts: z.number().optional(),
});
export type RunCounts = z.infer<typeof RunCountsSchema>;

export const RunSummarySchema = z.object({
  id: z.string(),
  batch_label: z.string(),
  policy_version_id: z.string().optional(),
  policy_version: z.number().optional(),
  counts: RunCountsSchema,
  duration_ms: z.number().nullable().optional(),
  created_at: z.string(),
});
export type RunSummary = z.infer<typeof RunSummarySchema>;

export function createRun(batch: "day1" | "day2") {
  return request(`/runs`, RunSummarySchema, {
    method: "POST",
    body: JSON.stringify({ batch }),
  });
}

export function listRuns() {
  return request(`/runs`, z.array(RunSummarySchema));
}

export function getRun(runId: string) {
  return request(`/runs/${runId}`, RunSummarySchema);
}

export function getRunReport(runId: string) {
  return request(`/runs/${runId}/report`, z.record(z.string(), z.unknown()));
}

// ---- Live run stream (SSE) ----

export const DecisionEventSchema = z.object({
  id: z.string(),
  counterparty_name: z.string().nullable().optional(),
  counterparty: z.string().nullable().optional(),
  amount: z.number().optional(),
  simulated: z.boolean().optional(),
  receipt_id: z.string().optional(),
  outcome: z.enum(["settled", "escalated"]),
  rule_id: z.string().nullable().optional(),
  matched_by: z.string().nullable().optional(),
  reason_code: z.string().nullable().optional(),
});
export type DecisionEvent = z.infer<typeof DecisionEventSchema>;

export const DoneEventSchema = z.object({
  id: z.string().optional(),
  counts: RunCountsSchema,
});
export type DoneEvent = z.infer<typeof DoneEventSchema>;

export function streamUrl(runId: string) {
  return `${BASE_URL}/runs/${runId}/stream`;
}

// ---- Decisions ----

export const DecisionRowSchema = z.object({
  id: z.string(),
  receipt_id: z.string(),
  counterparty_name: z.string().optional(),
  amount: z.number().optional(),
  simulated: z.boolean().optional(),
  outcome: z.string(),
  rule_id: z.string().nullable().optional(),
  matched_by: z.string().nullable().optional(),
  reason_code: z.string().nullable().optional(),
  evidence: z.unknown().optional(),
  reviewed_by: z.string().nullable().optional(),
  reviewed_at: z.string().nullable().optional(),
  review_outcome: z.string().nullable().optional(),
  policy_version: z.number().nullable().optional(),
  created_at: z.string().optional(),
});
export type DecisionRow = z.infer<typeof DecisionRowSchema>;

const PageSchema = <T extends z.ZodTypeAny>(item: T) =>
  z.object({
    items: z.array(item),
    total: z.number(),
    page: z.number().optional(),
    page_size: z.number().optional(),
  });

export function listDecisions(runId?: string, page = 0, pageSize = 50) {
  const params = new URLSearchParams();
  if (runId) params.set("run_id", runId);
  params.set("limit", String(pageSize));
  params.set("offset", String(page * pageSize));
  return request(`/decisions?${params}`, PageSchema(DecisionRowSchema));
}

// ---- Exceptions ----

export const ExceptionRowSchema = z.object({
  id: z.string(),
  decision_id: z.string(),
  receipt_id: z.string().optional(),
  source_receipt_id: z.string(),
  reference: z.string(),
  counterparty: z.string(),
  amount_centavos: z.number().int(),
  received_at: z.string(),
  payment_type: z.string().nullable(),
  customer_state: z.string().nullable(),
  seller_locations: z.array(
    z.object({ seller_id: z.string(), city: z.string(), state: z.string() }),
  ),
  simulated: z.boolean().optional(),
  reason_code: z.string(),
  evidence: z.unknown().optional(),
  status: z.string(),
  resolved_by: z.string().nullable().optional(),
  resolution: z.string().nullable().optional(),
  policy_version: z.number().int().optional(),
  created_at: z.string().optional(),
});
export type ExceptionRow = z.infer<typeof ExceptionRowSchema>;

export function listExceptions(opts: { runId?: string; status?: string; singletonsOnly?: boolean } = {}) {
  const params = new URLSearchParams({ limit: "200" });
  if (opts.runId) params.set("run_id", opts.runId);
  if (opts.status) params.set("status", opts.status);
  if (opts.singletonsOnly) params.set("singletons_only", "true");
  return request(`/exceptions?${params}`, PageSchema(ExceptionRowSchema));
}

// ---- Clusters ----

export const ClusterSchema = z.object({
  id: z.string(),
  run_id: z.string(),
  kind: z.enum(["escalated", "absorbed", "mixed", "rescued"]),
  source: z.enum(["code", "claude"]),
  cause: z.string().nullable(),
  confidence: z.number().nullable(),
  computed_summary: z.string(),
  member_count: z.number(),
  money_centavos: z.number().int().optional(),
  absorbed_centavos: z.number().int().optional(),
  explanation_status: z.enum(["explained", "not_yet_explained"]),
  status: z.string(),
});
export type Cluster = z.infer<typeof ClusterSchema>;

export const ClusterMemberSchema = z.object({
  receipt_id: z.string(),
  source_receipt_id: z.string(),
  reference: z.string(),
  counterparty: z.string().optional(),
  amount_centavos: z.number().int().optional(),
  received_at: z.string(),
  payment_type: z.string().nullable(),
  customer_state: z.string().nullable(),
  seller_locations: z.array(
    z.object({ seller_id: z.string(), city: z.string(), state: z.string() }),
  ),
  simulated: z.boolean().optional(),
  reason_code: z.string().nullable().optional(),
  adjustment_reason: z.string().nullable().optional(),
  matched_by: z.string().nullable().optional(),
  features: z.record(z.string(), z.unknown()).optional(),
  evidence: z.record(z.string(), z.unknown()).optional(),
});
export type ClusterMember = z.infer<typeof ClusterMemberSchema>;

export const FixSchema = z.object({
  id: z.string(),
  cluster_id: z.string(),
  type: z.string(),
  version: z.number(),
  params: z.record(z.string(), z.unknown()),
  summary: z.string().nullable(),
  is_widening: z.boolean(),
  is_alternative: z.boolean().optional(),
  proposed_by: z.string(),
  status: z.string(),
  snapshot_hash: z.string().nullable().optional(),
});
export type Fix = z.infer<typeof FixSchema>;

export const ClusterDetailSchema = ClusterSchema.extend({
  member_ids: z.array(z.string()).optional(),
  members: z.array(ClusterMemberSchema).optional(),
  fixes: z.array(FixSchema).optional(),
  evidence_cited: z.unknown().optional(),
});
export type ClusterDetail = z.infer<typeof ClusterDetailSchema>;

export function groupRun(runId: string) {
  return request(`/runs/${runId}/group`, z.array(ClusterSchema), { method: "POST" });
}

export function listClusters(opts: { runId?: string; status?: string } = {}) {
  const params = new URLSearchParams();
  if (opts.runId) params.set("run_id", opts.runId);
  if (opts.status) params.set("status", opts.status);
  return request(`/clusters?${params}`, z.array(ClusterSchema));
}

export function getCluster(id: string) {
  return request(`/clusters/${id}`, ClusterDetailSchema);
}

export function explainCluster(id: string) {
  return request(`/clusters/${id}/explain`, ClusterDetailSchema, { method: "POST" });
}

export function explainAll(runId: string) {
  return request(`/runs/${runId}/explain-all`, z.array(ClusterSchema), { method: "POST" });
}

// ---- Fixes ----

export const DryRunSchema = z.object({
  fix_id: z.string(),
  fix_version: z.number(),
  predicted: z.object({
    matched: z.number(),
    still_failing: z.number(),
    applied_centavos: z.number(),
    written_off_centavos: z.number(),
  }),
  side_effects: z.array(
    z.object({
      receipt_id: z.string(),
      counterparty_id: z.string(),
      amount_centavos: z.number(),
      would_become: z.string(),
      write_off_centavos: z.number(),
    }),
  ),
  snapshot_hash: z.string(),
  stale: z.boolean(),
});
export type DryRun = z.infer<typeof DryRunSchema>;

export function dryRunFix(fixId: string) {
  return request(`/fixes/${fixId}/dry-run`, DryRunSchema, { method: "POST" });
}

export function editFix(fixId: string, params: Record<string, unknown>, actorName: string) {
  return request(`/fixes/${fixId}/edit`, FixSchema, {
    method: "POST",
    body: JSON.stringify({ params, actor_name: actorName }),
  });
}

export const ApprovalResultSchema = z.object({
  fix: FixSchema,
  approvals: z.array(
    z.object({
      role: z.string(),
      name: z.string(),
      decision: z.string(),
      note: z.string().nullable().optional(),
      created_at: z.string(),
    }),
  ),
  needs_second_approval: z.boolean(),
  stale: z.boolean(),
});
export type ApprovalResult = z.infer<typeof ApprovalResultSchema>;

export function approveFix(fixId: string, role: string, name: string, note: string) {
  return request(`/fixes/${fixId}/approve`, ApprovalResultSchema, {
    method: "POST",
    body: JSON.stringify({ role, name, note }),
  });
}

export function rejectFix(fixId: string, role: string, name: string, note: string) {
  return request(`/fixes/${fixId}/reject`, FixSchema, {
    method: "POST",
    body: JSON.stringify({ role, name, note }),
  });
}

export const ApplyResultSchema = z.object({
  fix: FixSchema,
  verification: z.object({
    prediction_matched: z.boolean(),
    invariants: z.array(z.object({ name: z.string(), passed: z.boolean(), detail: z.string().nullable() })),
    outcome: z.enum(["verified", "verification_failed"]),
  }).nullable(),
  stale: z.boolean().default(false),
});
export type ApplyResult = z.infer<typeof ApplyResultSchema>;

export function applyFix(fixId: string) {
  return request(`/fixes/${fixId}/apply`, ApplyResultSchema, { method: "POST" });
}

export function resolveException(id: string, action: string, note: string, name: string, receivableId?: string) {
  return request(`/exceptions/${id}/resolve`, z.record(z.string(), z.unknown()), {
    method: "POST",
    body: JSON.stringify({ action, note, name, receivable_id: receivableId }),
  });
}

// ---- Ledger / recurrence / policy ----

export const LedgerEventSchema = z.object({
  id: z.string(),
  actor: z.string(),
  action: z.string(),
  entity_type: z.string(),
  entity_id: z.string(),
  before: z.unknown().nullable().optional(),
  after: z.unknown().nullable().optional(),
  created_at: z.string(),
});
export type LedgerEvent = z.infer<typeof LedgerEventSchema>;

export function listLedger(opts: { actor?: string; entityType?: string; runId?: string; page?: number } = {}) {
  const params = new URLSearchParams();
  if (opts.actor) params.set("actor", opts.actor);
  if (opts.entityType) params.set("entity_type", opts.entityType);
  if (opts.runId) params.set("run_id", opts.runId);
  if (opts.page) params.set("page", String(opts.page));
  return request(`/ledger?${params}`, PageSchema(LedgerEventSchema));
}

export const RecurrenceEntrySchema = z.object({
  fix_id: z.string(),
  fix_type: z.string(),
  summary: z.string().nullable(),
  condition: z.string(),
  samples: z.array(
    z.object({
      run_id: z.string(),
      batch_label: z.string(),
      policy_version: z.number().int().optional(),
      appeared: z.number(),
      auto_handled: z.number(),
      needed_human: z.number(),
    }),
  ),
});
export type RecurrenceEntry = z.infer<typeof RecurrenceEntrySchema>;

export function listRecurrence() {
  return request(`/recurrence`, z.array(RecurrenceEntrySchema));
}

export const PolicySchema = z.object({
  current: z.object({
    version: z.number(),
    rules: z.record(z.string(), z.record(z.string(), z.unknown())),
  }),
  versions: z.array(
    z.object({
      version: z.number(),
      created_at: z.string(),
      created_by: z.string(),
      fix_id: z.string().nullable().optional(),
    }),
  ),
});
export type Policy = z.infer<typeof PolicySchema>;

export function getPolicy() {
  return request(`/policy`, PolicySchema);
}

export function policyExportUrl() {
  return `${BASE_URL}/policy/export`;
}

export { BASE_URL };

export function proposeManualFix(clusterId: string, type: string, params: Record<string, unknown>, actorName: string) {
  return request(`/clusters/${clusterId}/fixes`, FixSchema, {
    method: "POST", body: JSON.stringify({ type, params, actor_name: actorName }),
  });
}
