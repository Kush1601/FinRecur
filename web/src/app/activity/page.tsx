"use client";

import { useEffect, useState } from "react";
import { listLedger, listRecurrence, type LedgerEvent, type RecurrenceEntry } from "@/lib/api";
import NotAvailable from "@/components/NotAvailable";
import {
  describeLedgerEvent,
  formatActor,
  formatDateTime,
  humanizeCode,
  shortReference,
} from "@/lib/presentation";
import { onRunUpdated } from "@/lib/runEvents";

type Tab = "ledger" | "recurrence";

export default function ActivityPage() {
  const [tab, setTab] = useState<Tab>("ledger");
  const [ledger, setLedger] = useState<LedgerEvent[] | null>(null);
  const [ledgerTotal, setLedgerTotal] = useState(0);
  const [ledgerPage, setLedgerPage] = useState(1); // backend pages are 1-indexed
  const LEDGER_PAGE_SIZE = 50;
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [recurrence, setRecurrence] = useState<RecurrenceEntry[] | null>(null);
  const [recurrenceError, setRecurrenceError] = useState<string | null>(null);
  const [actor, setActor] = useState("");
  const [entityType, setEntityType] = useState("");

  async function loadLedger() {
    const res = await listLedger({
      actor: actor || undefined,
      entityType: entityType || undefined,
      page: ledgerPage,
    });
    if (res.ok) {
      setLedger(res.data.items);
      setLedgerTotal(res.data.total);
      setLedgerError(null);
    } else {
      setLedger(null);
      setLedgerError(res.error);
    }
  }

  async function loadRecurrence() {
    const res = await listRecurrence();
    if (res.ok) {
      setRecurrence(res.data);
      setRecurrenceError(null);
    } else {
      setRecurrence(null);
      setRecurrenceError(res.error);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLedgerPage(1);
  }, [actor, entityType]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadLedger();
    void loadRecurrence();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actor, entityType, ledgerPage]);

  useEffect(() => {
    return onRunUpdated(() => {
      void loadLedger();
      void loadRecurrence();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="max-w-6xl mx-auto flex flex-col gap-5 page-enter">
      <div>
        <p className="page-kicker">Audit and recurrence</p>
        <h1 className="page-title">Activity</h1>
        <p className="page-subtitle">Trace every system and reviewer action, then confirm whether approved fixes keep recurring conditions under control.</p>
      </div>

      <div className="segmented text-[13px]">
        {(["ledger", "recurrence"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            data-active={tab === t}
          >
            {t === "ledger" ? "Ledger" : "Recurrence"}
          </button>
        ))}
      </div>

      {tab === "ledger" && (
        <div className="flex flex-col gap-3">
          <div className="flex gap-2">
            <input placeholder="Filter by actor" value={actor} onChange={(e) => setActor(e.target.value)} />
            <input
              placeholder="Filter by entity type"
              value={entityType}
              onChange={(e) => setEntityType(e.target.value)}
            />
          </div>
          {ledgerError && <NotAvailable label={`Could not load ledger: ${ledgerError}`} />}
          {ledger && (
            <div className="activity-feed">
              {ledger.length === 0 && <p style={{ color: "var(--ink-dim)" }}>No events match these filters.</p>}
              {ledger.map((event) => (
                <LedgerCard key={event.id} event={event} />
              ))}
            </div>
          )}
          {ledgerTotal > 0 && (
            <div className="flex items-center gap-2 text-[12px]" style={{ color: "var(--ink-dim)" }}>
              <span>
                Showing {(ledgerPage - 1) * LEDGER_PAGE_SIZE + 1}–
                {Math.min(ledgerTotal, ledgerPage * LEDGER_PAGE_SIZE)} of {ledgerTotal} events
              </span>
              <button
                onClick={() => setLedgerPage((p) => Math.max(1, p - 1))}
                disabled={ledgerPage <= 1}
              >
                Previous
              </button>
              <button
                onClick={() => setLedgerPage((p) => p + 1)}
                disabled={ledgerPage * LEDGER_PAGE_SIZE >= ledgerTotal}
              >
                Next
              </button>
            </div>
          )}
        </div>
      )}

      {tab === "recurrence" && (
        <div className="flex flex-col gap-3">
          {recurrenceError && <NotAvailable label={`Could not load recurrence: ${recurrenceError}`} />}
          {recurrence && recurrence.length === 0 && !recurrenceError && (
            <p style={{ color: "var(--ink-dim)" }}>
              No fixes have been applied yet. Recurrence tracking starts once a fix is applied,
              and shows results once a later batch has run against it.
            </p>
          )}
          {recurrence &&
            recurrence.map((r) => (
              <div key={r.fix_id} className="card p-3">
                <div className="flex items-center gap-2">
                  <span className="tag tag-readable">{humanizeCode(r.fix_type)}</span>
                  <div className="font-medium">{r.summary}</div>
                </div>
                {r.samples.length === 0 ? (
                  <p style={{ color: "var(--ink-dim)" }}>
                    Applied, but no later batch has run yet to check whether it held.
                  </p>
                ) : (
                <div className="recurrence-samples">
                  {r.samples.map((s) => (
                    <div key={s.run_id} className="recurrence-sample">
                      <div className="flex items-end gap-[2px]" style={{ height: 40 }}>
                        <Bar value={s.auto_handled} max={s.appeared} color="var(--accent-settled)" />
                        <Bar value={s.needed_human} max={s.appeared} color="var(--accent-escalated)" />
                      </div>
                      <div>
                        <strong className="mono">{s.batch_label}</strong>
                        <div>{s.appeared} appeared</div>
                        <div>{s.auto_handled} automatic · {s.needed_human} human</div>
                      </div>
                    </div>
                  ))}
                </div>
                )}
                <details className="mt-2">
                  <summary style={{ cursor: "pointer", color: "var(--ink-dim)" }}>Technical recurrence condition</summary>
                  <code className="mono text-[12px]">{r.condition}</code>
                </details>
              </div>
            ))}
        </div>
      )}
    </div>
  );
}

function LedgerCard({ event }: { event: LedgerEvent }) {
  const narrative = describeLedgerEvent(event);
  return (
    <article className="card ledger-card">
      <div className="ledger-marker" aria-hidden="true" />
      <div className="ledger-content">
        <div className="ledger-heading">
          <div>
            <h2>{narrative.title}</h2>
            <p>{narrative.summary}</p>
          </div>
          <time dateTime={event.created_at}>{formatDateTime(event.created_at)}</time>
        </div>
        <div className="ledger-meta">
          <span>{formatActor(event.actor)}</span>
          <span>{humanizeCode(event.entity_type)} {shortReference(event.entity_id)}</span>
        </div>
        <details>
          <summary>Technical audit record</summary>
          <div className="technical-record">
            <div><span>Entity</span><code>{event.entity_type}/{event.entity_id}</code></div>
            <div><span>Before</span><code>{JSON.stringify(event.before) ?? "None"}</code></div>
            <div><span>After</span><code>{JSON.stringify(event.after) ?? "None"}</code></div>
          </div>
        </details>
      </div>
    </article>
  );
}

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  const h = max > 0 ? Math.max(2, (value / max) * 40) : 2;
  return <div style={{ width: 10, height: h, background: color }} title={String(value)} />;
}
