import { formatBRL } from "./format";

const LABELS: Record<string, string> = {
  add_counterparty_alias: "Add counterparty alias",
  adjust: "Balance adjustment recorded",
  allocate: "Receipt allocated",
  amend_policy: "Amend matching policy",
  apply: "Approved fix applied",
  approve: "Fix approved",
  approve_stale: "Approval blocked by changed data",
  apply_stale: "Apply blocked by changed data",
  dry_run: "Dry run completed",
  dry_run_failed: "Dry run blocked",
  edit_fix: "Fix parameters revised",
  explain: "Cause explained",
  explain_skipped: "Explanation unavailable",
  fee_deducted: "Processing fee deducted",
  group: "Exception pattern grouped",
  manual_review: "Keep for manual review",
  mark_duplicate: "Mark duplicate receipt",
  mixed: "Mixed outcomes",
  no_match: "No matching receivable",
  one_receipt_many_receivables: "One receipt covers several orders",
  overpayment: "Overpayment",
  partial_payment_pending: "Partial payment pending",
  payment_on_cancelled: "Payment on cancelled order",
  propose_fix: "Fix proposed",
  record_fee_deduction: "Record processing fee",
  reject: "Fix rejected",
  repair_reference: "Repair payment reference",
  short_pay: "Short payment",
  split_receipt: "Split receipt across orders",
  suspected_duplicate: "Possible duplicate payment",
  tolerance: "Within policy tolerance",
  verification_failed: "Verification failed",
};

export function humanizeCode(value: string): string {
  if (LABELS[value]) return LABELS[value];
  const words = value.replaceAll("_", " ").trim();
  return words ? `${words[0].toUpperCase()}${words.slice(1)}` : "Unknown";
}

export function shortReference(value: string, length = 8): string {
  if (!value) return "Not available";
  if (value.length <= length + 1) return value;
  return `${value.slice(0, length)}…`;
}

export function displayCounterparty(value?: string): string {
  if (!value) return "Unknown customer";
  const match = /^Customer\s+([a-z0-9]+)$/i.exec(value);
  return match ? `Customer •••${match[1]}` : value;
}

export function formatPaymentType(value: string | null | undefined): string {
  return value ? humanizeCode(value) : "Payment method unavailable";
}

type SellerLocation = { city: string; state: string };

export function formatSellerLocations(locations: SellerLocation[]): string {
  if (locations.length === 0) return "Seller location unavailable";
  if (locations.length > 1) return `${locations.length} seller locations`;
  const [location] = locations;
  const city = location.city.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  return `${city}, ${location.state.toUpperCase()}`;
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatActor(value: string): string {
  if (value === "system") return "System";
  if (value === "agent") return "Matching engine";
  if (value === "claude") return "AI analysis";
  return value;
}

type LedgerLike = {
  action: string;
  after?: unknown;
};

type LedgerNarrative = {
  title: string;
  summary: string;
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function number(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

export function describeLedgerEvent(event: LedgerLike): LedgerNarrative {
  const after = record(event.after);

  if (event.action === "dry_run") {
    const predicted = record(after.predicted);
    const matched = number(predicted.matched);
    const affected = number(predicted.applied_centavos);
    const adjustment = number(predicted.written_off_centavos);
    const sideEffects = number(after.side_effect_count);
    return {
      title: LABELS.dry_run,
      summary: [
        matched !== null ? `${matched} receipts would match` : null,
        affected !== null ? `${formatBRL(affected)} affected` : null,
        adjustment !== null ? `${formatBRL(adjustment)} adjustment` : null,
        sideEffects === 0 ? "No side effects" : sideEffects !== null ? `${sideEffects} side effects` : null,
      ].filter(Boolean).join(" · "),
    };
  }

  if (event.action === "group") {
    const count = number(after.member_count);
    return {
      title: LABELS.group,
      summary: `${count ?? "Several"} related transactions · ${humanizeCode(String(after.kind ?? "pattern"))}`,
    };
  }

  if (event.action === "propose_fix") {
    return {
      title: LABELS.propose_fix,
      summary: humanizeCode(String(after.type ?? "fix")),
    };
  }

  if (event.action === "explain") {
    const confidence = number(after.confidence);
    return {
      title: LABELS.explain,
      summary: `${String(after.cause ?? "Cause recorded")}${confidence !== null ? ` · ${Math.round(confidence * 100)}% confidence` : ""}`,
    };
  }

  if (event.action === "allocate" || event.action === "adjust") {
    const amount = number(after.amount);
    const reason = typeof after.reason === "string" ? ` · ${humanizeCode(after.reason)}` : "";
    return {
      title: LABELS[event.action],
      summary: `${amount !== null ? formatBRL(amount) : "Amount recorded"}${reason}`,
    };
  }

  if (event.action === "reject" && after.reason === "out_of_bounds") {
    return {
      title: "Policy change blocked",
      summary: typeof after.detail === "string" ? after.detail : "The proposed change exceeded the policy limit.",
    };
  }

  if (event.action === "approve") {
    const role = typeof after.role === "string" ? humanizeCode(after.role) : "Reviewer";
    const note = typeof after.note === "string" && after.note ? ` · ${after.note}` : "";
    return { title: LABELS.approve, summary: `${role} approval recorded${note}` };
  }

  if (event.action === "apply") {
    return {
      title: LABELS.apply,
      summary: "The approved change was committed and verified.",
    };
  }

  return {
    title: humanizeCode(event.action),
    summary: "Audit event recorded. Open technical details for the complete payload.",
  };
}
