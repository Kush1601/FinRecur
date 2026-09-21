"use client";

import { useEffect, useState } from "react";
import { getPolicy, policyExportUrl, type Policy } from "@/lib/api";
import NotAvailable from "@/components/NotAvailable";
import { formatDateTime, humanizeCode } from "@/lib/presentation";

const RULE_LABELS: Record<string, string> = {
  R1: "Exact order reference",
  R2: "Amount and date window",
  R3: "Short-payment threshold",
  R4: "Percentage tolerance",
  R5: "Grouped payment",
  R6: "Overpayment",
  R7: "Rounding tolerance",
  R8: "Escalate for review",
};

export default function PolicyPage() {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const res = await getPolicy();
      if (res.ok) setPolicy(res.data);
      else setError(res.error);
    })();
  }, []);

  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-5 page-enter">
      <div className="page-heading-row flex items-center justify-between">
        <div>
          <p className="page-kicker">Decision controls</p>
          <h1 className="page-title">Policy</h1>
          <p className="page-subtitle">The active rule set is versioned, attributable, and exportable for independent review.</p>
        </div>
        <a href={policyExportUrl()} className="primary mono">
          Export JSON ↓
        </a>
      </div>

      {error && <NotAvailable label={`Could not load policy: ${error}`} />}

      {policy && (
        <>
          <table>
            <thead>
              <tr>
                <th>Rule</th>
                <th>Values</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(policy.current.rules).map(([ruleId, values]) => (
                <tr key={ruleId}>
                  <td className="mono">{ruleId}</td>
                  <td>
                    <div className="font-medium">{RULE_LABELS[ruleId] ?? ruleId}</div>
                    <div className="policy-values">
                      {Object.entries(values)
                        .filter(([key]) => key !== "source" && key !== "kind")
                        .map(([key, value]) => (
                          <span key={key}>{humanizeCode(key)}: <strong>{formatPolicyValue(key, value)}</strong></span>
                        ))}
                      {Object.keys(values).every((key) => key === "source" || key === "kind") && (
                        <span>No configurable threshold</span>
                      )}
                    </div>
                  </td>
                  <td className="text-[12px]" style={{ color: "var(--ink-dim)" }}>
                    {typeof values.source === "string" ? values.source : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <section>
            <h2 className="font-semibold mb-2">Version history</h2>
            <table>
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Created by</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {policy.versions.map((v) => (
                  <tr key={v.version}>
                    <td className="mono">v{v.version}</td>
                    <td>{v.created_by === "seed" ? "Demo setup" : v.created_by}</td>
                    <td>{formatDateTime(v.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}

function formatPolicyValue(key: string, value: unknown): string {
  if (key === "threshold_centavos" || key === "tolerance_centavos") {
    return `R$ ${(Number(value) / 100).toFixed(2)}`;
  }
  if (key === "max_percent") return `${value}%`;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}
