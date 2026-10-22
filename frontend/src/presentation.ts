import type { Purchase } from "./api";

// Display exact backend decimal strings. Never round or calculate money in JS.
export function dollars(value: string | undefined): string {
  if (value === undefined) return "—";
  const match = /^(-?)(\d+)(\.\d+)?$/.exec(value);
  if (!match) return "Unavailable";
  const fraction = (match[3] ?? ".").padEnd(3, "0");
  return `${match[1]}$${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}`;
}

export const words = (value: string) =>
  value.toLowerCase().replaceAll("_", " ");

export function proposalState(
  item: Pick<
    Purchase,
    "eligibility" | "initial_decision" | "expired" | "policy_current"
  >,
) {
  const status = item.eligibility.status;
  if (status === "DENIED") return { label: "Denied", tone: "danger" };
  if (status === "BLOCKED") {
    if (item.expired) return { label: "Expired", tone: "neutral" };
    if (!item.policy_current)
      return { label: "Policy changed", tone: "warning" };
    return {
      label: item.initial_decision === "REJECTED" ? "Rejected" : "Blocked",
      tone: "danger",
    };
  }
  if (status === "AWAITING_HUMAN_APPROVAL")
    return { label: "Needs your approval", tone: "warning" };
  if (status === "APPROVED_PENDING_DISPATCH_CHECKS")
    return { label: "Approved by you", tone: "success" };
  if (status === "AUTO_AUTHORIZED_PENDING_DISPATCH_CHECKS")
    return { label: "Auto-authorized", tone: "brand" };
  if (status === "DISPATCH_STARTED")
    return { label: "Dispatch started", tone: "warning" };
  return { label: "Review required", tone: "warning" };
}

export function reasonLabel(code: string) {
  const labels: Record<string, string> = {
    HUMAN_APPROVAL_REQUIRED: "Above your auto-approval limit",
    AUTO_APPROVAL_THRESHOLD_MET: "Within your auto-approval limit",
    HARD_PURCHASE_LIMIT_EXCEEDED: "Exceeds your purchase limit",
    INSUFFICIENT_BUDGET: "Exceeds available budget",
    MERCHANT_NOT_ALLOWED: "Merchant is not on your allowlist",
    PROPOSAL_REJECTED: "Rejected by policy",
    PROPOSAL_EXPIRED: "Proposal has expired",
    POLICY_INVALIDATED: "A newer policy is active",
    HUMAN_DENIED: "Denied by the Owner",
    OFFER_INVALIDATED: "Product offer is no longer valid",
    ALREADY_DISPATCHED: "Dispatch already started",
  };
  return labels[code] ?? words(code);
}
