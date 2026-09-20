"use client";

import { useEffect, useState } from "react";
import { getPolicy, policyExportUrl, type Policy } from "@/lib/api";
import NotAvailable from "@/components/NotAvailable";

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
                  <td className="mono text-[12px]">
                    {Object.entries(values)
                      .filter(([k]) => k !== "source")
                      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                      .join(", ")}
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
                    <td>{v.created_by}</td>
                    <td className="mono">{v.created_at}</td>
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
