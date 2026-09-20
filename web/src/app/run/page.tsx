"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  createRun,
  explainAll,
  groupRun,
  listDecisions,
  listRuns,
  streamUrl,
  DecisionEventSchema,
  DoneEventSchema,
  type DecisionEvent,
  type RunCounts,
  type RunSummary,
} from "@/lib/api";
import { formatBRL } from "@/lib/format";

type Row = {
  key: string;
  counterparty: string;
  amount: number | null;
  simulated: boolean;
  outcome: string;
  rule: string | null;
  matchedBy: string | null;
  reasonCode: string | null;
};

function toRow(ev: DecisionEvent): Row {
  return {
    key: ev.id,
    counterparty: ev.counterparty_name ?? ev.counterparty ?? "—",
    amount: ev.amount ?? null,
    simulated: ev.simulated ?? false,
    outcome: ev.outcome,
    rule: ev.rule_id ?? null,
    matchedBy: ev.matched_by ?? null,
    reasonCode: ev.reason_code ?? null,
  };
}

export default function RunPage() {
  const [run, setRun] = useState<RunSummary | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [counters, setCounters] = useState<RunCounts>({});
  const [phase, setPhase] = useState<"loading" | "idle" | "streaming" | "done">("loading");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [starting, setStarting] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const startRef = useRef<number>(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  async function bootstrap() {
    const res = await listRuns();
    if (res.ok && res.data.length > 0) {
      const latest = res.data[0];
      setRun(latest);
      const dec = await listDecisions(latest.id, 0, 200);
      if (dec.ok) {
        setRows(
          dec.data.items.map((d) => ({
            key: d.id,
            counterparty: d.counterparty_name ?? "—",
            amount: d.amount ?? null,
            simulated: d.simulated ?? false,
            outcome: d.outcome,
            rule: d.rule_id ?? null,
            matchedBy: d.matched_by ?? null,
            reasonCode: d.reason_code ?? null,
          })),
        );
      }
      setCounters(latest.counts);
      setPhase("done");
    } else {
      setPhase("idle");
    }
  }

  useEffect(() => {
// eslint-disable-next-line react-hooks/set-state-in-effect
    void bootstrap();
    return () => {
      esRef.current?.close();
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  function startCounters() {
    startRef.current = Date.now();
    timerRef.current = setInterval(() => setElapsedMs(Date.now() - startRef.current), 100);
  }

  function stopCounters() {
    if (timerRef.current) clearInterval(timerRef.current);
  }

  function beginStream(newRun: RunSummary) {
    setRun(newRun);
    setRows([]);
    setCounters({});
    setPhase("streaming");
    startCounters();

    const es = new EventSource(streamUrl(newRun.id));
    esRef.current = es;

    es.addEventListener("decision", (e) => {
      const parsed = DecisionEventSchema.safeParse(JSON.parse((e as MessageEvent).data));
      if (!parsed.success) return;
      const row = toRow(parsed.data);
      setRows((prev) => [...prev, row]);
      setCounters((prev) => ({
        ...prev,
        matched: (prev.matched ?? 0) + (row.outcome === "settled" ? 1 : 0),
        escalated: (prev.escalated ?? 0) + (row.outcome === "escalated" ? 1 : 0),
      }));
    });

    es.addEventListener("done", async (e) => {
      const parsed = DoneEventSchema.safeParse(JSON.parse((e as MessageEvent).data));
      if (parsed.success) {
        setCounters(parsed.data.counts);
      }
      es.close();
      await prepareReview(newRun.id);
      stopCounters();
      setPhase("done");
    });

    es.onerror = () => {
      es.close();
      stopCounters();
      setPhase("done");
    };
  }

  async function runBatch() {
    setStarting(true);
    const batch = run ? "day2" : "day1";
    const res = await createRun(batch);
    setStarting(false);
    if (res.ok) beginStream(res.data);
  }

  async function skipAnimation() {
    esRef.current?.close();
    stopCounters();
    if (!run) return;
    const dec = await listDecisions(run.id, 0, 200);
    if (dec.ok) {
      setRows(
        dec.data.items.map((d) => ({
          key: d.id,
          counterparty: d.counterparty_name ?? "—",
          amount: d.amount ?? null,
          simulated: d.simulated ?? false,
          outcome: d.outcome,
          rule: d.rule_id ?? null,
          matchedBy: d.matched_by ?? null,
          reasonCode: d.reason_code ?? null,
        })),
      );
    }
    await prepareReview(run.id);
    setPhase("done");
  }

  async function prepareReview(runId: string) {
    const grouped = await groupRun(runId);
    if (grouped.ok) await explainAll(runId);
  }

  const showTable = phase === "streaming" || phase === "done";

  return (
    <div className="max-w-6xl mx-auto flex flex-col gap-5 page-enter">
      <div className="page-heading-row flex items-center justify-between">
        <div>
          <p className="page-kicker">Operations / matching engine</p>
          <h1 className="page-title">Run</h1>
          <p className="page-subtitle">Process the current receipt batch, then inspect every match and exception before review.</p>
        </div>
        {phase === "idle" && (
          <button className="primary" onClick={runBatch} disabled={starting}>
            {starting ? "Starting…" : "Run day 1"}
          </button>
        )}
        {phase === "done" && (
          <button onClick={runBatch} disabled={starting}>
            {starting ? "Starting…" : "Run again"}
          </button>
        )}
        {phase === "streaming" && (
          <button onClick={skipAnimation}>skip animation</button>
        )}
      </div>

      {phase === "loading" && <p style={{ color: "var(--ink-dim)" }}>Loading…</p>}

      {phase === "idle" && (
        <p style={{ color: "var(--ink-dim)" }}>
          No run yet. Running processes the seeded batch through the rule engine and streams each
          decision as it happens.
        </p>
      )}

      {run && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-[12px]">
          <Counter label="Matched" value={String(counters.matched ?? 0)} />
          <Counter label="Escalated" value={String(counters.escalated ?? 0)} />
          <Counter label="Applied" value={formatBRL(counters.applied_centavos ?? 0)} />
          <Counter label="Written off" value={formatBRL(counters.written_off_centavos ?? 0)} />
          <Counter
            label="Elapsed"
            value={phase === "streaming" ? `${(elapsedMs / 1000).toFixed(1)}s` : `${run.duration_ms ?? 0}ms`}
          />
        </div>
      )}

      {showTable && (
        <table>
          <thead>
            <tr>
              <th>Counterparty</th>
              <th className="num">Amount</th>
              <th>Outcome</th>
              <th>Rule</th>
              <th>Evidence strength</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <td>
                  {r.counterparty} {r.simulated && <span className="tag tag-simulated">simulated</span>}
                </td>
                <td className="num mono">{r.amount !== null ? formatBRL(r.amount) : "—"}</td>
                <td>
                  <span className={`tag ${r.outcome === "settled" ? "badge-settled" : "badge-escalated"}`}>
                    {r.outcome}
                  </span>
                </td>
                <td className="mono">{r.rule ?? r.reasonCode ?? "—"}</td>
                <td className="mono">{r.matchedBy ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {phase === "done" && run && (
        <div className="flex items-center justify-between card p-4">
          <div>
            <div className="font-medium">
              {run.batch_label}: {counters.total_receipts ?? rows.length} receipts,{" "}
              {counters.matched ?? 0} matched, {counters.escalated ?? 0} escalated
            </div>
            <div className="mono" style={{ color: "var(--ink-dim)" }}>
              applied {formatBRL(counters.applied_centavos ?? 0)} &middot; written off{" "}
              {formatBRL(counters.written_off_centavos ?? 0)}
            </div>
          </div>
          <Link href="/review" className="primary" style={{ display: "inline-flex", alignItems: "center" }}>
            Go to review queue
          </Link>
        </div>
      )}
    </div>
  );
}

function Counter({ label, value }: { label: string; value: string }) {
  return (
    <div className="card metric-card px-4 py-3">
      <div style={{ color: "var(--ink-dim)", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: 16 }}>
        {value}
      </div>
    </div>
  );
}
