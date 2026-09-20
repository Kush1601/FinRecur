export default function AboutPage() {
  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-7 page-enter">
      <div className="about-intro">
        <p className="page-kicker">System boundaries</p>
        <h1 className="page-title">What FinRecur actually is</h1>
        <p className="page-subtitle">An honest accounting automation demo: real transaction structure, clearly marked simulated faults, and deterministic controls around every AI suggestion.</p>
      </div>

      <div className="about-grid">
      <section className="card info-panel flex flex-col gap-2">
        <h2 className="display" style={{ fontSize: 20 }}>
          What&apos;s real
        </h2>
        <p>
          The underlying transactions are a sampled slice of real Olist (Brazilian e-commerce,
          2016-18) orders, order items, payments, customers and sellers, imported with{" "}
          <code className="mono">customer_unique_id</code> as identity. The rule engine, grouping,
          fix menu, dry run, approval, apply, and invariant checks all run against that data.
          Genuine unexplained discrepancies in the slice are left unresolved rather than
          explained away.
        </p>
      </section>

      <section className="card info-panel flex flex-col gap-2">
        <h2 className="display" style={{ fontSize: 20 }}>
          What&apos;s simulated
        </h2>
        <p>
          A documented set of faults (missing counterparty aliases, corrupted references,
          duplicate imports, freight-adjacent short-pays, processor fees, split payments, pending
          instalments) is injected at import time so the demo has known causes to find. The
          Reviewer / Approver role switch is a cookie, not authentication. Generated dates, names
          and reference numbers carry a <span className="tag tag-simulated">simulated</span> tag
          wherever they appear.
        </p>
      </section>

      <section className="card info-panel flex flex-col gap-2">
        <h2 className="display" style={{ fontSize: 20 }}>
          What&apos;s out of scope
        </h2>
        <p>FX, chargebacks, credit notes, deposits, and real authentication are not modelled.</p>
      </section>

      <section className="card info-panel flex flex-col gap-2">
        <h2 className="display" style={{ fontSize: 20 }}>
          The LLM boundary
        </h2>
        <p>
          Claude is called in exactly two places: naming the cause of a code-found cluster, and
          picking a fix type and its parameters from a fixed menu. Structured features go in
          (reason codes, ratios, edit distances); Claude never sees raw memo text as an
          instruction, and its output is validated first against a schema, then against the
          actual rows — every id it cites must exist and belong to the group it was given; it may
          drop members but never add one. Claude never writes to the database, never approves a
          fix, and never grades its own work.
        </p>
      </section>

      <section className="card info-panel flex flex-col gap-2">
        <h2 className="display" style={{ fontSize: 20 }}>
          What a production version would add
        </h2>
        <p>
          Real authentication and segregation of duties, a monthly write-off cap with alerting,
          a review-outcomes report distinguishing overrides from errors, multi-currency and
          intercompany support, and an untrusted-inbox gate for any fix sourced from vendor
          correspondence.
        </p>
      </section>
      </div>
    </div>
  );
}
