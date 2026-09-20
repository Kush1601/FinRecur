"use client";

import { useEffect, useState } from "react";
import { listLedger, listRecurrence, type LedgerEvent, type RecurrenceEntry } from "@/lib/api";
import NotAvailable from "@/components/NotAvailable";

type Tab = "ledger" | "recurrence";

export default function ActivityPage() {
  const [tab, setTab] = useState<Tab>("ledger");
  const [ledger, setLedger] = useState<LedgerEvent[] | null>(null);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [recurrence, setRecurrence] = useState<RecurrenceEntry[] | null>(null);
  const [recurrenceError, setRecurrenceError] = useState<string | null>(null);
  const [actor, setActor] = useState("");
  const [entityType, setEntityType] = useState("");

  async function loadLedger() {
    const res = await listLedger({ actor: actor || undefined, entityType: entityType || undefined });
    if (res.ok) {
      setLedger(res.data.items);
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
    void loadLedger();
    void loadRecurrence();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actor, entityType]);

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
            <table style={{ tableLayout: "fixed", width: "100%" }}>
              <colgroup>
                <col style={{ width: "10%" }} />
                <col style={{ width: "12%" }} />
                <col style={{ width: "20%" }} />
                <col style={{ width: "43%" }} />
                <col style={{ width: "15%" }} />
              </colgroup>
              <thead>
                <tr>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Entity</th>
                  <th>Before → after</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((e) => (
                  <tr key={e.id}>
                    <td className="mono">{e.actor}</td>
                    <td>{e.action}</td>
                    <td className="mono" style={{ overflowWrap: "anywhere" }}>
                      {e.entity_type}/{e.entity_id}
                    </td>
                    <td
                      className="mono text-[12px]"
                      style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}
                    >
                      {JSON.stringify(e.before)} → {JSON.stringify(e.after)}
                    </td>
                    <td className="mono" style={{ overflowWrap: "anywhere" }}>{e.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === "recurrence" && (
        <div className="flex flex-col gap-3">
          {recurrenceError && <NotAvailable label={`Could not load recurrence: ${recurrenceError}`} />}
          {recurrence &&
            recurrence.map((r) => (
              <div key={r.fix_id} className="card p-3">
                <div className="font-medium">{r.summary}</div>
                <div className="mono text-[12px]" style={{ color: "var(--ink-dim)" }}>
                  {r.condition}
                </div>
                <div className="flex gap-3 mt-2">
                  {r.samples.map((s) => (
                    <div key={s.run_id} className="flex flex-col items-center text-[11px]">
                      <div className="flex items-end gap-[2px]" style={{ height: 40 }}>
                        <Bar value={s.auto_handled} max={s.appeared} color="var(--accent-settled)" />
                        <Bar value={s.needed_human} max={s.appeared} color="var(--accent-escalated)" />
                      </div>
                      <div className="mono">{s.batch_label}</div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
        </div>
      )}
    </div>
  );
}

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  const h = max > 0 ? Math.max(2, (value / max) * 40) : 2;
  return <div style={{ width: 10, height: h, background: color }} title={String(value)} />;
}
