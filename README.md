# FinRecur

FinRecur finds why reconciliation exceptions keep coming back, proposes a typed fix, proves it on
the affected transactions, and checks whether the condition returns.

It is a working sketch of the “Improves” step in a finance agent’s
Learns → Runs → Escalates → Improves loop. The focus is change control: a language model may explain
a code-found pattern and choose from a fixed fix menu, but code performs the dry run, approval gate,
apply, invariant checks, and recurrence measurement.

The interface is designed as a reconciliation control room rather than a generic analytics
dashboard. A persistent status rail keeps the active batch, receipt volume, matched count,
exceptions, and policy version visible. The review workspace then follows the decision sequence:
cause → supporting transactions → proposed fix → dry-run impact → approval → verification.

## What is real, simulated, and out of scope

**Real.** Orders, items, freight values, and payment records come from the public
[Olist Brazilian e-commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
(anonymised commercial data, 2016–2018). The demo imports a one-month slice and uses
`customer_unique_id` as counterparty identity. The rule engine, grouping, dry runs, approvals,
ledger, and invariant checks run against PostgreSQL. Genuine unexplained Olist discrepancies stay
unresolved unless the available evidence supports a diagnosis.

**Simulated.** Seven fault families are injected at seed time from a hidden manifest: a missing
counterparty alias, corrupted references, duplicate imports, freight-adjacent short-pays, processor
fees, one transfer covering several orders, and pending instalments. Injected rows carry
`simulated=true`. Generated names and dates are marked the same way. Reviewer and Approver are a
cookie-based role switch for demonstrating the workflow; they are not authentication or real
segregation of duties. Olist payment rows stand in for receipts, not bank statement lines, and each
inherits its customer from the original order. That makes corrupted-reference cases easier than a
real bank feed would be.

**Out of scope.** Foreign currency, chargebacks, credit notes, deposits, ERP or bank connectors,
real authentication, encryption at rest, and signed audit logs.

## The workflow

1. **Run.** A fixed policy evaluates receipts in the order R5 group → R1 exact → R2 amount/date →
   R7 rounding → R3 short-pay → R4 small-shortfall tolerance → R6 overpayment → R8 escalate.
2. **Group.** Code computes ratios, reference distance, duplicate fingerprints, seller overlap,
   and allocation candidates over open exceptions *and* automatic write-offs. A permissive policy
   therefore cannot hide a repeated fee leak.
3. **Explain and propose.** Claude names the cause and chooses one primary fix, plus at most one
   alternative, from F1–F7. Without an API key, committed cache fixtures replay the demo; a cache
   miss leaves the cluster honestly labelled “Grouped by code · not yet explained.”
4. **Dry-run.** Code applies the fix to a copy and reports matched rows, rows still failing, money
   applied, money written off, and additional write-offs outside the cluster.
5. **Approve and apply.** Data fixes need one reviewer. A widening policy amendment needs a second,
   distinct approver. Approval is tied to the fix version and a hash of balances, allocations,
   statuses, aliases, and rule settings. Changed data refuses the apply.
6. **Verify and watch.** The apply runs in a transaction. Prediction equality and ledger invariants
   must pass or the transaction rolls back. Stored condition signatures are evaluated only against
   incoming rows in later batches and report appeared / auto-handled / needed-human.

### Review workspace

Every cluster keeps its source transactions beside the proposed change. Simulated rows are visibly
labelled, and the review queue leads with shortened order/payment references, anonymised customers,
payment method, and seller-to-customer geography. Internal UUIDs and raw payloads remain available
inside expandable technical records instead of dominating the decision view. Money uses tabular
figures, widening changes require a second approver, and Apply remains disabled until the required
approval state is present. The Activity screen provides a plain-language view over the append-only
ledger plus Day 2 recurrence samples; Policy shows readable rules and version history.

## The LLM boundary

Claude is called in exactly two places: `grouping.explain` names a cause and confirms membership;
`fixes.propose` selects a fix and parameters from the fixed F1–F7 menu. It receives structured
features, not raw memo text. Tool output is validated against a Pydantic schema and then against the
actual group: every receipt, receivable, counterparty, and reference target must be one code already
offered. Claude may drop candidate members but cannot add one.

Claude cannot write to the database, approve a fix, apply it, or grade the result. The dry-run,
widening bounds, two-person gate, snapshot check, transaction, and invariants are deterministic
code. No `ANTHROPIC_API_KEY` is required for the seeded workflow or CI.

## Evaluation

`make eval` first reseeds a deterministic Day 1 dataset, then scores it against `data/faults.json`,
which the application cannot import. It checks observed rule outcomes, code-found membership,
cached explanations, proposed fix types, dry-run/verification agreement, and invariant failures.
The current seeded slice produces:

| Fault | Rows observed | Code-grouped | Proposed fix |
| --- | ---: | ---: | --- |
| A · counterparty variant | 5 | 5 | `add_counterparty_alias` |
| B · corrupted reference | 14 | 14 | `repair_reference` |
| C · duplicate import | 9 | 0 | none; fingerprint pairs are below the 3-row cluster floor |
| D · seller shortfall | 6 | 6 | `amend_policy` |
| E · 2.5% processor fee | 11 | 11 | `record_fee_deduction` |
| F · one transfer, many orders | 3 | 3 | `split_receipt` |
| G · pending instalment | 0 on Day 1 | 0 | withheld for Day 2 |

The current run processes 1,244 receipts: 1,165 settle and 79 escalate. Code finds six candidate
groups; cached responses explain all six. There are zero recorded invariant failures. Fault A’s
nominal scenario asked for 18 rows, but this real monthly slice contains only five usable payment
rows for the selected repeat customer; the manifest records and gates on that observed fact instead
of claiming the intended number.

Generated details are written to `eval_report.json` and `eval_report.md`. They are ignored by Git so
each run reflects the local database rather than a pasted artifact.

## Run it

Requirements: Docker, Node.js 22, and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
cd backend && uv sync && cd ..
cd web && npm ci && cd ..

make seed       # starts PostgreSQL, applies Alembic migrations, imports Olist + demo faults
```

Then start the API and web app in separate terminals:

```bash
make api        # terminal 1 · http://localhost:8000
make web        # terminal 2 · http://localhost:3000
```

Open `http://localhost:3000`, run Day 1, review a cluster, dry-run and approve its fix, then run Day
2 and open Activity → Recurrence.

Project checks:

```bash
make lint       # ruff, formatting, pyright, ESLint, TypeScript
make test       # pytest + Vitest
make eval       # reseed + hidden-label evaluation gate
make e2e        # reseed, start API/web through Playwright, exercise Day 1 → fix → Day 2
```

Current verification baseline:

- 185 backend tests passing.
- 11 frontend tests passing.
- Production Next.js build passing.
- Full Playwright workflow passing: Day 1 → alias approval → verified apply → Day 2 recurrence.
- Evaluation gate passing with zero invariant failures.

## Repository map

- `backend/finrecur/` — pure rules, grouping, fix, dry-run, snapshot, and invariant logic.
- `backend/api/` — FastAPI routes, SQLAlchemy models, and transaction-owning services.
- `backend/alembic/` — schema source of truth.
- `web/` — responsive Next.js reconciliation control room and browser workflow tests.
- `scripts/seed.py` — the setup script allowed to read and inject the fault manifest.
- `scripts/eval.py` — the separate gold-label evaluator.
- `backend/tests/fixtures/claude/` — input-hash keyed responses used in CI without a key.

## A production version would add

Real identity and role enforcement, signed audit events, encryption at rest, policy-level monthly
write-off caps, review-outcomes reporting, connector idempotency, concurrent-run locking, FX and
multi-entity accounting, and an untrusted-content gate before any action derived from email or an
uploaded document.
