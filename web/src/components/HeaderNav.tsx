"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { createRun, explainAll, groupRun, listRuns, type RunSummary } from "@/lib/api";
import { readRole, writeRole, type Role } from "@/lib/role";
import { emitRunUpdated, onRunUpdated } from "@/lib/runEvents";

const NAV = [
  { href: "/run", label: "Runs" },
  { href: "/review", label: "Review queue" },
  { href: "/activity", label: "Activity" },
  { href: "/policy", label: "Policy" },
  { href: "/about", label: "About" },
];

export default function HeaderNav() {
  const pathname = usePathname();
  const router = useRouter();
  const [latest, setLatest] = useState<RunSummary | null>(null);
  const [role, setRole] = useState<Role>(() => readRole().role);
  const [name, setName] = useState(() => readRole().name);
  const [editingName, setEditingName] = useState(false);
  const [running, setRunning] = useState(false);

  async function refreshLatest() {
    const res = await listRuns();
    if (res.ok && res.data.length > 0) {
      setLatest(res.data[0]);
    }
  }

  useEffect(() => {
// eslint-disable-next-line react-hooks/set-state-in-effect
    void refreshLatest();
    return onRunUpdated(() => {
      void refreshLatest();
    });
  }, []);

  function commitRole(nextRole: Role, nextName: string) {
    setRole(nextRole);
    setName(nextName);
    writeRole({ role: nextRole, name: nextName || (nextRole === "reviewer" ? "Reviewer" : "Approver") });
  }

  async function runNextBatch() {
    setRunning(true);
    const batch = latest ? "day2" : "day1";
    const res = await createRun(batch);
    setRunning(false);
    if (res.ok) {
      const grouped = await groupRun(res.data.id);
      if (grouped.ok) await explainAll(res.data.id);
      setLatest(res.data);
      emitRunUpdated();
      router.push("/run");
    }
  }

  const receiptCount = latest?.counts.total_receipts ?? null;
  const policyVersion = latest?.policy_version ?? null;
  const matchedCount = latest?.counts.matched ?? null;
  const escalatedCount = latest?.counts.escalated ?? null;

  return (
    <header className="app-header">
      <div className="app-header-main">
        <Link href="/" className="brand" aria-label="FinRecur home">
          <span className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span>
            <strong>FinRecur</strong>
            <small>Reconciliation control</small>
          </span>
        </Link>

        <nav className="app-nav" aria-label="Primary navigation">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname?.startsWith(item.href + "/");
            return (
              <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined}>
                {item.label}
                {item.href === "/review" && (escalatedCount ?? 0) > 0 && (
                  <span className="nav-count">{escalatedCount}</span>
                )}
              </Link>
            );
          })}
        </nav>

        <div className="role-control">
          <span className="role-label">Demo role</span>
          <select
            aria-label="Simulated role"
            value={role}
            onChange={(e) => commitRole(e.target.value as Role, name)}
            className="compact-control"
          >
            <option value="reviewer">Reviewer</option>
            <option value="approver">Approver</option>
          </select>
          {editingName ? (
            <input
              aria-label="Simulated reviewer name"
              className="compact-control w-28"
              value={name}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onBlur={() => {
                commitRole(role, name);
                setEditingName(false);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  commitRole(role, name);
                  setEditingName(false);
                }
              }}
            />
          ) : (
            <button onClick={() => setEditingName(true)} className="compact-control reviewer-name">
              {name}
            </button>
          )}
        </div>

        <button className="primary run-action" onClick={runNextBatch} disabled={running}>
          {running ? "Starting…" : latest ? "Run next batch →" : "Run day 1 →"}
        </button>
      </div>

      <div className="status-rail" aria-label="Current reconciliation status">
        <StatusItem label="Batch" value={latest?.batch_label ?? "Not started"} />
        <StatusItem label="Receipts" value={receiptCount?.toLocaleString() ?? "—"} />
        <StatusItem label="Matched" value={matchedCount?.toLocaleString() ?? "—"} tone="success" />
        <StatusItem label="Exceptions" value={escalatedCount?.toLocaleString() ?? "—"} tone="alert" />
        <StatusItem label="Policy" value={policyVersion !== null ? `v${policyVersion}` : "—"} />
        <div className="status-stage">
          <span className="status-pulse" />
          {latest ? "Ready for review" : "Waiting for first batch"}
        </div>
      </div>
    </header>
  );
}

function StatusItem({ label, value, tone }: { label: string; value: string; tone?: "success" | "alert" }) {
  return (
    <div className={`status-item ${tone ? `status-${tone}` : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
