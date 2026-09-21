"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  approveFix,
  dryRunFix,
  editFix,
  explainCluster,
  getCluster,
  proposeManualFix,
  rejectFix,
  applyFix,
  type ApplyResult,
  type ClusterDetail,
  type DryRun,
  type Fix,
} from "@/lib/api";
import { formatBRL } from "@/lib/format";
import {
  displayCounterparty,
  formatDateTime,
  formatPaymentType,
  formatSellerLocations,
  humanizeCode,
  shortReference,
} from "@/lib/presentation";
import NotAvailable from "@/components/NotAvailable";
import { useRole } from "@/lib/role";

const KIND_CLASS: Record<string, string> = {
  escalated: "badge-escalated",
  absorbed: "badge-absorbed",
  mixed: "badge-mixed",
  rescued: "badge-rescued",
};

export default function ClusterDetailClient({ id }: { id: string }) {
  const [cluster, setCluster] = useState<ClusterDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [explaining, setExplaining] = useState(false);

  async function load() {
    // Keep an already-rendered fix panel mounted during action refreshes. Its
    // mount hook creates the dry-run preview; remounting after approval would
    // rerun that hook and incorrectly move the fix back to `dry_run`.
    if (!cluster) setLoading(true);
    const res = await getCluster(id);
    if (res.ok) {
      setCluster(res.data);
      setError(null);
    } else {
      setError(res.error);
    }
    setLoading(false);
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  async function handleExplain() {
    setExplaining(true);
    const res = await explainCluster(id);
    setExplaining(false);
    if (res.ok) setCluster(res.data);
  }

  if (loading) return <p style={{ color: "var(--ink-dim)" }}>Loading…</p>;
  if (error || !cluster) return <NotAvailable label={`Could not load cluster: ${error ?? "unknown"}`} />;

  const evidence = cluster.evidence_cited as Record<string, unknown> | undefined;
  const memberIds = cluster.member_ids ?? [];
  const fixes = cluster.fixes ?? [];
  const currentFixes = fixes.filter((f) => f.status !== "superseded");
  const primaryFix = [...currentFixes]
    .filter((f) => !f.is_alternative)
    .sort((a, b) => b.version - a.version)[0];
  const altFix = [...currentFixes]
    .filter((f) => f.is_alternative)
    .sort((a, b) => b.version - a.version)[0];
  const members = cluster.members ?? [];

  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-5 page-enter">
      <Link href="/review" className="text-[12px] font-medium" style={{ color: "var(--accent-primary)" }}>
        ← Back to review queue
      </Link>

      <div className="flex items-center gap-2">
        <div>
          <p className="page-kicker">Exception cluster / {cluster.member_count} members</p>
          <div className="flex items-center gap-2">
            <h1 className="page-title">Review the evidence</h1>
            <span className={`tag tag-readable ${KIND_CLASS[cluster.kind] ?? ""}`}>
              {humanizeCode(cluster.kind)}
            </span>
            {cluster.source === "code" && <span className="tag">code</span>}
          </div>
        </div>
        <span className="ml-auto num mono font-semibold text-xl">{formatBRL(cluster.money_centavos ?? 0)}</span>
      </div>

      <section className="card p-4 flex flex-col gap-2">
        <h2 className="font-semibold">Cause</h2>
        {cluster.cause ? (
          <p>{cluster.cause}</p>
        ) : (
          <div className="flex flex-col gap-2">
            <p style={{ color: "var(--ink-dim)" }}>
              Grouped by code · not yet explained — {cluster.computed_summary}
            </p>
            {cluster.source === "code" && (
              <button onClick={handleExplain} disabled={explaining} style={{ alignSelf: "flex-start" }}>
                {explaining ? "Requesting…" : "Request explanation"}
              </button>
            )}
          </div>
        )}
        {evidence !== undefined && (
          <details>
            <summary style={{ cursor: "pointer", color: "var(--ink-dim)" }}>
              Technical evidence and identifiers
            </summary>
            <pre className="mono text-[12px] p-2 mt-2" style={{ background: "var(--paper)", border: "1px solid var(--rule)" }}>
              {JSON.stringify(evidence, null, 2)}
            </pre>
          </details>
        )}
      </section>

      <section className="card p-4 flex flex-col gap-2">
        <h2 className="font-semibold">Members ({memberIds.length})</h2>
        {members.length === 0 ? (
          <p style={{ color: "var(--ink-dim)" }}>No member detail available.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Order / payment</th>
                <th>Customer</th>
                <th>Payment</th>
                <th>Route</th>
                <th>Reason</th>
                <th className="num">Amount</th>
                <th className="num">Difference</th>
              </tr>
            </thead>
            <tbody>
              {members.map((member) => (
                <tr key={member.receipt_id}>
                  <td>
                    <div className="mono font-medium">Order {shortReference(member.reference)}</div>
                    <div className="table-secondary mono">
                      Payment {shortReference(member.source_receipt_id)} · {formatDateTime(member.received_at)}
                    </div>
                    {member.simulated && <span className="tag tag-simulated mt-1">simulated scenario</span>}
                  </td>
                  <td>{displayCounterparty(member.counterparty)}</td>
                  <td>{formatPaymentType(member.payment_type)}</td>
                  <td>
                    <div>{formatSellerLocations(member.seller_locations)}</div>
                    <div className="table-secondary">Customer state {member.customer_state ?? "unavailable"}</div>
                  </td>
                  <td>{humanizeCode(member.reason_code ?? member.adjustment_reason ?? member.matched_by ?? "unknown")}</td>
                  <td className="num mono">{formatBRL(member.amount_centavos ?? 0)}</td>
                  <td className="num mono">
                    {typeof member.features?.shortfall_centavos === "number"
                      ? formatBRL(member.features.shortfall_centavos)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card p-4 flex flex-col gap-2">
        <h2 className="font-semibold">Proposed fix</h2>
        {primaryFix ? (
          <FixBlock key={primaryFix.id} fix={primaryFix} altFix={altFix} onChanged={load} />
        ) : (
          <ManualFixForm clusterId={id} onProposed={load} />
        )}
      </section>
    </div>
  );
}

function FixBlock({ fix, altFix, onChanged }: { fix: Fix; altFix?: Fix; onChanged: () => Promise<void> }) {
  const [showAlt, setShowAlt] = useState(false);
  const active = showAlt && altFix ? altFix : fix;
  const [dryRun, setDryRun] = useState<DryRun | null>(null);
  const [dryRunError, setDryRunError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null);
  const [paramsText, setParamsText] = useState(JSON.stringify(active.params, null, 2));
  const [editError, setEditError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  // Reactive: a role switch in the header (Reviewer <-> Approver, or a name
  // change) must be reflected here without a remount, or an approval taken
  // right after switching roles is silently attributed to the old one.
  const role = useRole();
  const [staleNotice, setStaleNotice] = useState<string | null>(null);

  async function runDryRun() {
    const res = await dryRunFix(active.id);
    if (res.ok) {
      setDryRun(res.data);
      setDryRunError(null);
    } else {
      setDryRunError(res.error);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void runDryRun();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active.id]);

  async function approve() {
    setActing(true);
    const res = await approveFix(active.id, role.role, role.name, note);
    if (res.ok) {
      setActionError(null);
      if (res.data.stale) {
        // The backend refused to record this signature against a snapshot
        // that no longer matches -- nothing was approved. Re-preview before
        // letting the reviewer try again, the same recovery apply() already had.
        setStaleNotice("Data changed since this preview. Approval was not recorded.");
        await runDryRun();
      } else {
        setStaleNotice(null);
        await onChanged();
      }
    } else {
      setActionError(res.error);
    }
    setActing(false);
  }

  async function reject() {
    setActing(true);
    const res = await rejectFix(active.id, role.role, role.name, note);
    if (res.ok) {
      setActionError(null);
      await onChanged();
    } else {
      setActionError(res.error);
    }
    setActing(false);
  }

  async function apply() {
    setActing(true);
    const res = await applyFix(active.id);
    if (res.ok) {
      setActionError(null);
      setApplyResult(res.data);
      if (res.data.stale) void runDryRun();
    } else {
      setActionError(res.error);
    }
    setActing(false);
  }

  async function saveEdit() {
    let params: Record<string, unknown>;
    try {
      params = JSON.parse(paramsText) as Record<string, unknown>;
    } catch {
      setEditError("Parameters must be valid JSON.");
      return;
    }
    const res = await editFix(active.id, params, role.name);
    if (res.ok) {
      setEditError(null);
      await onChanged();
    } else {
      setEditError(res.error);
    }
  }

  function toggleAlternative() {
    const nextShowAlt = !showAlt;
    const nextFix = nextShowAlt && altFix ? altFix : fix;
    setShowAlt(nextShowAlt);
    setParamsText(JSON.stringify(nextFix.params, null, 2));
  }

  const showApproverGate = active.is_widening && role.role === "approver";

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="tag tag-readable">{humanizeCode(active.type)}</span>
        <span className="tag tag-readable">{active.status === "dry_run" ? "Ready to review" : humanizeCode(active.status)}</span>
        {active.is_widening && <span className="tag badge-absorbed">widening</span>}
        {altFix && (
          <button onClick={toggleAlternative} className="ml-auto">
            {showAlt ? "View primary fix" : "View alternative fix"}
          </button>
        )}
      </div>

      <p>{active.summary}</p>

      {active.type === "amend_policy" ? (
        <PolicyDiffBlock params={active.params} />
      ) : (
        <ParamsBlock params={active.params} />
      )}

      {role.role === "reviewer" && active.status !== "applied" && (
        <details>
          <summary style={{ cursor: "pointer", color: "var(--ink-dim)" }}>Edit parameters</summary>
          <div className="flex flex-col gap-2 mt-2">
            <textarea
              className="mono text-[12px]"
              rows={10}
              value={paramsText}
              onChange={(event) => setParamsText(event.target.value)}
            />
            <button onClick={saveEdit} style={{ alignSelf: "flex-start" }}>Save as new version</button>
            {editError && <NotAvailable label={editError} />}
          </div>
        </details>
      )}

      {dryRunError && <NotAvailable label={`Dry run not available: ${dryRunError}`} />}

      {dryRun && (
        <div className="flex flex-col gap-2">
          <div className="grid grid-cols-4 gap-2 text-[12px]">
            <Stat label="Would settle" value={String(dryRun.predicted.matched)} />
            <Stat label="Still needs review" value={String(dryRun.predicted.still_failing)} />
            <Stat label="Affected value" value={formatBRL(dryRun.predicted.applied_centavos)} />
            <Stat label="Fee / write-off" value={formatBRL(dryRun.predicted.written_off_centavos)} />
          </div>

          {dryRun.side_effects.length > 0 && (
            <div>
              <h3 className="text-[12px]" style={{ color: "var(--ink-dim)" }}>
                Additional write-offs you&apos;d be accepting
              </h3>
              <table>
                <thead>
                  <tr>
                    <th>Receipt</th>
                    <th>Counterparty</th>
                    <th className="num">Amount</th>
                    <th>Would become</th>
                    <th className="num">Write-off</th>
                  </tr>
                </thead>
                <tbody>
                  {dryRun.side_effects.map((s) => (
                    <tr key={s.receipt_id}>
                      <td className="mono">{shortReference(s.receipt_id)}</td>
                      <td className="mono">{shortReference(s.counterparty_id)}</td>
                      <td className="num mono">{formatBRL(s.amount_centavos)}</td>
                      <td>{s.would_become}</td>
                      <td className="num mono">{formatBRL(s.write_off_centavos)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {dryRun.stale ? (
            <div className="flex items-center gap-2">
              <p style={{ color: "var(--accent-escalated)" }}>
                Data changed since this preview. Run the check again before approving.
              </p>
              <button onClick={runDryRun}>Re-run</button>
            </div>
          ) : applyResult ? (
            <VerificationStrip result={applyResult} />
          ) : (
            <FixActions
              role={role.role}
              showApproverGate={showApproverGate}
              note={note}
              setNote={setNote}
              approve={approve}
              reject={reject}
              apply={apply}
              canApply={active.status === "approved"}
              acting={acting}
            />
          )}
          {staleNotice && <NotAvailable label={staleNotice} />}
          {actionError && <NotAvailable label={actionError} />}
        </div>
      )}
    </div>
  );
}

function FixActions({
  role,
  showApproverGate,
  note,
  setNote,
  approve,
  reject,
  apply,
  canApply,
  acting,
}: {
  role: string;
  showApproverGate: boolean;
  note: string;
  setNote: (v: string) => void;
  approve: () => Promise<void>;
  reject: () => Promise<void>;
  apply: () => Promise<void>;
  canApply: boolean;
  acting: boolean;
}) {
  return (
    <div className="flex flex-col gap-2">
      <textarea placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} />
      <div className="flex gap-2">
        {role === "reviewer" && (
          <>
            <button className="primary" onClick={approve} disabled={acting || note.trim().length === 0}>
              Approve
            </button>
            <button onClick={reject} disabled={acting || note.trim().length === 0}>
              Reject with note
            </button>
          </>
        )}
        {showApproverGate && (
          <button className="primary" onClick={approve} disabled={acting || note.trim().length === 0}>
            Give second signature
          </button>
        )}
        <button onClick={apply} disabled={acting || !canApply}>Apply approved fix</button>
      </div>
      {!showApproverGate && role === "approver" && (
        <p style={{ color: "var(--ink-dim)", fontSize: 12 }}>
          Approver signature is only requested for widening fixes.
        </p>
      )}
    </div>
  );
}

function VerificationStrip({ result }: { result: ApplyResult }) {
  if (!result.verification) {
    return (
      <div className="p-2" style={{ background: "var(--accent-warn-bg)", color: "var(--accent-warn)" }}>
        {result.stale
          ? "Data changed since this preview. Run the check again before approving."
          : "Apply did not return a verification result."}
      </div>
    );
  }
  const ok = result.verification.outcome === "verified";
  const passed = result.verification.invariants.filter((i) => i.passed).length;
  const total = result.verification.invariants.length;
  return (
    <div
      className="p-2"
      style={{
        background: ok ? "var(--accent-settled-bg)" : "var(--accent-escalated-bg)",
        color: ok ? "var(--accent-settled)" : "var(--accent-escalated)",
      }}
    >
      {ok
        ? `Verified · prediction matched · ${passed}/${total} invariants passed`
        : `Verification failed · rolled back · ${result.verification.invariants
            .filter((i) => !i.passed)
            .map((i) => i.name)
            .join(", ")}`}
    </div>
  );
}

function PolicyDiffBlock({ params }: { params: Record<string, unknown> }) {
  // F6 amend_policy params are {rule_id, key, before, after, justification} --
  // before/after are the rule's own scalar value (e.g. 2.9), not an object of
  // several keys. Treating them as a settings object made these boxes render
  // empty for every real amend_policy fix.
  const ruleId = typeof params.rule_id === "string" ? params.rule_id : null;
  const key = typeof params.key === "string" ? params.key : null;
  const label = ruleId && key ? `${ruleId}.${key}` : (ruleId ?? key ?? "value");
  const before = params.before;
  const after = params.after;
  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-2 gap-2 text-[12px]">
        <div className="mono p-2" style={{ background: "var(--paper)", border: "1px solid var(--rule)" }}>
          <div style={{ color: "var(--ink-dim)" }}>Before</div>
          <div>
            {label}: {JSON.stringify(before)}
          </div>
        </div>
        <div className="mono p-2" style={{ background: "var(--accent-warn-bg)", border: "1px solid var(--rule)" }}>
          <div style={{ color: "var(--ink-dim)" }}>After</div>
          <div>
            {label}: {JSON.stringify(after)}
          </div>
        </div>
      </div>
      {typeof params.justification === "string" && <p>{params.justification}</p>}
    </div>
  );
}

function ParamsBlock({ params }: { params: Record<string, unknown> }) {
  return (
    <>
      <div className="decision-facts">
        {Object.entries(params).map(([key, value]) => (
          <div key={key} className="decision-fact">
            <span>{humanizeCode(key)}</span>
            <strong>{formatParameter(key, value)}</strong>
          </div>
        ))}
      </div>
      <details>
        <summary style={{ cursor: "pointer", color: "var(--ink-dim)" }}>Technical parameters</summary>
        <pre className="mono text-[12px] p-2 mt-2" style={{ background: "var(--paper)", border: "1px solid var(--rule)" }}>
          {JSON.stringify(params, null, 2)}
        </pre>
      </details>
    </>
  );
}

function formatParameter(key: string, value: unknown): string {
  if (key === "fee_percent" && typeof value === "number") return `${value}%`;
  if (key === "payment_type" && typeof value === "string") return formatPaymentType(value);
  if (Array.isArray(value)) return `${value.length} selected ${key === "receipt_ids" ? "receipts" : "items"}`;
  if (value !== null && typeof value === "object") return `${Object.keys(value).length} configured values`;
  if (typeof value === "string" && value.length > 20) return shortReference(value);
  return String(value);
}

const MANUAL_FIX_TYPES = [
  "add_counterparty_alias",
  "repair_reference",
  "mark_duplicate",
  "split_receipt",
  "record_fee_deduction",
  "amend_policy",
  "manual_review",
] as const;

function ManualFixForm({ clusterId, onProposed }: { clusterId: string; onProposed: () => Promise<void> }) {
  const role = useRole();
  const [type, setType] = useState<(typeof MANUAL_FIX_TYPES)[number]>("manual_review");
  const [paramsText, setParamsText] = useState('{\n  "reason": ""\n}');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit() {
    let params: Record<string, unknown>;
    try {
      params = JSON.parse(paramsText) as Record<string, unknown>;
    } catch {
      setError("Parameters must be valid JSON.");
      return;
    }
    setSubmitting(true);
    const res = await proposeManualFix(clusterId, type, params, role.name);
    setSubmitting(false);
    if (res.ok) {
      setError(null);
      await onProposed();
    } else {
      setError(res.error);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <p style={{ color: "var(--ink-dim)" }}>
        No fix has been proposed. Request an explanation above, or pick a fix type and its
        parameters yourself -- the same dry-run, approval, and verification steps apply either way.
      </p>
      <label className="flex flex-col gap-1 text-[12px]">
        Fix type
        <select
          className="compact-control"
          value={type}
          onChange={(e) => setType(e.target.value as (typeof MANUAL_FIX_TYPES)[number])}
        >
          {MANUAL_FIX_TYPES.map((t) => (
            <option key={t} value={t}>
              {humanizeCode(t)}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-[12px]">
        Parameters (JSON)
        <textarea
          className="mono text-[12px]"
          rows={6}
          value={paramsText}
          onChange={(e) => setParamsText(e.target.value)}
        />
      </label>
      <button onClick={submit} disabled={submitting} style={{ alignSelf: "flex-start" }}>
        {submitting ? "Proposing…" : "Propose this fix"}
      </button>
      {error && <NotAvailable label={error} />}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card px-3 py-2">
      <div style={{ color: "var(--ink-dim)", fontSize: 11 }}>{label}</div>
      <div className="mono">{value}</div>
    </div>
  );
}
