"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  explainAll,
  listClusters,
  listExceptions,
  listRuns,
  resolveException,
  type Cluster,
  type ExceptionRow,
} from "@/lib/api";
import { formatBRL, formatPercent } from "@/lib/format";
import {
  displayCounterparty,
  formatDateTime,
  formatPaymentType,
  formatSellerLocations,
  humanizeCode,
  shortReference,
} from "@/lib/presentation";
import NotAvailable from "@/components/NotAvailable";
import { readRole } from "@/lib/role";

type Tab = "clusters" | "singletons";

const KIND_CLASS: Record<string, string> = {
  escalated: "badge-escalated",
  absorbed: "badge-absorbed",
  mixed: "badge-mixed",
  rescued: "badge-rescued",
};

export default function ReviewPage() {
  const [tab, setTab] = useState<Tab>("clusters");
  const [runId, setRunId] = useState<string | null>(null);
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [exceptions, setExceptions] = useState<ExceptionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [explaining, setExplaining] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    const runs = await listRuns();
    const latest = runs.ok && runs.data.length > 0 ? runs.data[0] : null;
    setRunId(latest?.id ?? null);

    const [clusterRes, exceptionRes] = await Promise.all([
      listClusters(latest ? { runId: latest.id } : {}),
      listExceptions(
        latest
          ? { runId: latest.id, status: "open", singletonsOnly: true }
          : { status: "open", singletonsOnly: true },
      ),
    ]);
    if (clusterRes.ok) setClusters(clusterRes.data);
    if (exceptionRes.ok) setExceptions(exceptionRes.data.items);
    setLoading(false);
  }

  useEffect(() => {
// eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, []);

  async function handleExplainAll() {
    if (!runId) return;
    setExplaining(true);
    setError(null);
    const res = await explainAll(runId);
    setExplaining(false);
    if (res.ok) {
      setClusters(res.data);
    } else {
      setError(res.error);
    }
  }

  // Explained clusters (Claude has named a cause) lead, ranked by money at stake. Code-only
  // clusters follow, ranked by member count since money_centavos isn't populated by the
  // API yet.
  const sorted = [...clusters].sort((a, b) => {
    const aExplained = a.source === "claude";
    const bExplained = b.source === "claude";
    if (aExplained !== bExplained) return aExplained ? -1 : 1;
    if (aExplained) return (b.money_centavos ?? 0) - (a.money_centavos ?? 0);
    return b.member_count - a.member_count;
  });
  const singletonExceptions = exceptions;

  return (
    <div className="max-w-6xl mx-auto flex flex-col gap-5 page-enter">
      <div className="page-heading-row flex items-center justify-between">
        <div>
          <p className="page-kicker">Human decision queue</p>
          <h1 className="page-title">Needs your review</h1>
          <p className="page-subtitle">Review recurring patterns first; resolve one-off exceptions only when the evidence supports a decision.</p>
        </div>
        {tab === "clusters" && (
          <button onClick={handleExplainAll} disabled={!runId || explaining}>
            {explaining ? "Explaining…" : "Explain all"}
          </button>
        )}
      </div>

      <div className="segmented text-[13px]">
        {(["clusters", "singletons"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            data-active={tab === t}
          >
            {t === "clusters" ? "Clusters" : "Singletons"}
          </button>
        ))}
      </div>

      {error && <NotAvailable label={`Explain all failed: ${error}`} />}
      {loading && <p style={{ color: "var(--ink-dim)" }}>Loading…</p>}

      {!loading && !runId && (
        <p style={{ color: "var(--ink-dim)" }}>No run yet — start one on the Run page.</p>
      )}

      {!loading && runId && tab === "clusters" && (
        <div className="flex flex-col gap-2">
          {sorted.length === 0 && <p style={{ color: "var(--ink-dim)" }}>No clusters yet. Run and group a batch first.</p>}
          {sorted.map((c) => (
            <ClusterCard key={c.id} cluster={c} />
          ))}
        </div>
      )}

      {!loading && runId && tab === "singletons" && (
        <div className="flex flex-col gap-2">
          {singletonExceptions.length === 0 && (
            <p style={{ color: "var(--ink-dim)" }}>No open singleton exceptions.</p>
          )}
          {singletonExceptions.map((e) => (
            <SingletonRow key={e.id} exception={e} />
          ))}
        </div>
      )}
    </div>
  );
}

function ClusterCard({ cluster }: { cluster: Cluster }) {
  return (
    <Link href={`/review/clusters/${cluster.id}`} className="card cluster-card p-4 flex flex-col gap-2 no-underline">
      <div className="flex items-center gap-2">
        <span className={`tag tag-readable ${KIND_CLASS[cluster.kind] ?? ""}`}>
          {humanizeCode(cluster.kind)}
        </span>
        {cluster.source === "code" && <span className="tag">code</span>}
        <span className="ml-auto num mono font-medium">{formatBRL(cluster.money_centavos ?? 0)}</span>
      </div>
      <div className="text-[13px]">
        {cluster.cause ?? (
          <span style={{ color: "var(--ink-dim)" }}>Grouped by code · not yet explained — {cluster.computed_summary}</span>
        )}
      </div>
      <div className="flex gap-4 text-[12px] mono" style={{ color: "var(--ink-dim)" }}>
        <span>{cluster.member_count} members</span>
        {cluster.confidence !== null && <span>confidence {formatPercent(cluster.confidence, 0)}</span>}
      </div>
    </Link>
  );
}

function SingletonRow({ exception }: { exception: ExceptionRow }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [action, setAction] = useState("apply_to");
  const [targetId, setTargetId] = useState("");
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const evidence = (exception.evidence ?? {}) as Record<string, unknown>;
  const candidates = Array.isArray(evidence.candidate_receivables)
    ? (evidence.candidate_receivables as Array<Record<string, unknown>>)
    : [];

  async function submitResolution() {
    const role = readRole();
    const inferredTarget = targetId || (typeof candidates[0]?.id === "string" ? candidates[0].id : undefined);
    setSaving(true);
    setResolveError(null);
    const response = await resolveException(
      exception.id,
      action,
      note,
      role.name,
      inferredTarget,
    );
    setSaving(false);
    if (response.ok) {
      setResult(action === "leave_open" ? `Reviewed by ${role.name} · left open` : `Reviewed by ${role.name} · resolved`);
    } else {
      setResolveError(response.error);
    }
  }

  return (
    <div className="card singleton-card p-4 flex flex-col gap-3">
      <div className="singleton-heading">
        <div>
          <div className="flex items-center gap-2">
            <span className="tag tag-readable badge-escalated">{humanizeCode(exception.reason_code)}</span>
            {exception.simulated && <span className="tag tag-simulated">simulated scenario</span>}
          </div>
          <div className="singleton-reference">
            Order <span className="mono">{shortReference(exception.reference)}</span>
          </div>
        </div>
        <div className="singleton-status">
          {exception.resolved_by ? (
            <span>Reviewed by {exception.resolved_by}</span>
          ) : (
            <span>Detected automatically · Awaiting review</span>
          )}
          <button onClick={() => setOpen((o) => !o)}>{open ? "Close review" : "Review details"}</button>
        </div>
      </div>

      <div className="singleton-facts">
        <span>{displayCounterparty(exception.counterparty)}</span>
        <span>{formatPaymentType(exception.payment_type)}</span>
        <span>{formatSellerLocations(exception.seller_locations)} → Customer {exception.customer_state ?? "state unavailable"}</span>
        <span>{formatDateTime(exception.received_at)}</span>
        <strong className="mono">{formatBRL(exception.amount_centavos)}</strong>
      </div>

      {open && (
        <>
          <div className="review-context">
            <div>
              <span>Reason</span>
              <strong>{humanizeCode(exception.reason_code)}</strong>
            </div>
            <div>
              <span>Payment reference</span>
              <strong className="mono">{shortReference(exception.source_receipt_id, 12)}</strong>
            </div>
            <div>
              <span>Policy</span>
              <strong>Version {exception.policy_version}</strong>
            </div>
            <div>
              <span>Suggested matches</span>
              <strong>{candidates.length || "None"}</strong>
            </div>
          </div>

          <details>
            <summary style={{ cursor: "pointer", color: "var(--ink-dim)" }}>Technical evidence</summary>
            <pre className="mono text-[12px] p-2 mt-2" style={{ background: "var(--paper)", border: "1px solid var(--rule)" }}>
              {exception.evidence ? JSON.stringify(exception.evidence, null, 2) : "No technical evidence attached."}
            </pre>
          </details>

          <div className="flex flex-col gap-2">
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="apply_to">Apply to receivable</option>
              <option value="write_off">Write off</option>
              <option value="mark_duplicate">Mark duplicate</option>
              <option value="reject">Reject</option>
              <option value="leave_open">Leave open</option>
            </select>
            {action === "apply_to" || action === "mark_duplicate" ? (
              <input
                className="mono"
                placeholder={action === "mark_duplicate" ? "Earlier receipt id" : "Receivable id"}
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
              />
            ) : null}
            <textarea
              placeholder="Note (required)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <div className="flex items-center gap-2">
              <button
                className="primary"
                disabled={saving || note.trim().length === 0}
                onClick={submitResolution}
              >
                {saving ? "Saving…" : "Save review"}
              </button>
              {result && <span style={{ color: "var(--accent-settled)" }}>{result}</span>}
            </div>
            {resolveError && <NotAvailable label={`Could not save review: ${resolveError}`} />}
          </div>
        </>
      )}
    </div>
  );
}
