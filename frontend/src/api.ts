export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code.replaceAll("_", " "));
  }
}

export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//"))
    throw new Error("Same-origin API required");
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    const name =
      location.protocol === "https:"
        ? "__Host-agentpal_csrf"
        : "agentpal_csrf_dev";
    const cookie = document.cookie
      .split("; ")
      .find((item) => item.startsWith(name + "="));
    if (cookie) headers["X-CSRF-Token"] = cookie.slice(name.length + 1);
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    referrerPolicy: "no-referrer",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const code = data?.detail?.code;
    throw new ApiError(
      response.status,
      typeof code === "string" && /^[a-z0-9_]+$/.test(code)
        ? code
        : response.status === 401
          ? "authentication_required"
          : response.status === 403
            ? "request_rejected"
            : "request_failed",
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export interface Policy {
  id: string;
  monthly_limit: string;
  auto_approval_limit: string;
  hard_purchase_limit: string;
  purchase_ttl_seconds: number;
  merchant_allowlist: string[];
}
export interface Budget {
  available_amount: string;
  limit_amount: string;
  spent_amount: string;
  reserved_amount: string;
  next_reset_at: string;
}
export interface Purchase {
  id: string;
  title: string;
  merchant_domain: string;
  unit_price: string;
  total_amount: string;
  quantity: number;
  initial_decision: string;
  initial_reason_codes: string[];
  snapshot_hash: string;
  snapshot_hash_version: number;
  expires_at: string;
  policy_current: boolean;
  expired: boolean;
  payment_status: string;
  human_decision: { decision: string } | null;
  eligibility: {
    status: string;
    can_approve: boolean;
    can_deny: boolean;
    reason_codes: string[];
    execution_ready: false;
  };
}
export interface ShoppingStatus {
  mode: string;
  model_ready: boolean;
  discovery_ready: boolean;
  policy_ready: boolean;
  agent_ready: boolean;
  ready: boolean;
}
export interface ShoppingResult {
  mode: string;
  query: string;
  quantity: number;
  steps: string[];
  purchase: Purchase | null;
  candidates: {
    product_id: string;
    title: string;
    merchant_domain: string;
    unit_price: string;
    availability: string;
  }[];
}
export interface VaultStatus {
  connected: boolean;
  agreement_state?: string;
  onboarding: { state: string } | null;
}
