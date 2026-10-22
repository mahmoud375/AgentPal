import { StrictMode, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { api, ApiError } from "./api";
import type {
  Budget,
  Policy,
  Purchase,
  ShoppingResult,
  ShoppingStatus,
  VaultStatus,
} from "./api";
import "./style.css";
import { Icon, Logo, Message } from "./ui";
import type { IconName } from "./ui";
import { dollars, proposalState, reasonLabel, words } from "./presentation";

const NAVIGATION: { key: string; icon: IconName; label: string }[] = [
  { key: "shopping", icon: "agent", label: "Shopping agent" },
  { key: "proposals", icon: "proposals", label: "Proposals" },
  { key: "policy", icon: "policy", label: "Spending policy" },
  { key: "connection", icon: "connection", label: "PayPal connection" },
];

function Login({ onLogin }: { onLogin: (name: string) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const owner = await api<{ login_name: string }>("/auth/login", "POST", {
        login_name: data.get("login"),
        password: data.get("password"),
      });
      onLogin(owner.login_name);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 401
          ? "Login failed. Check your credentials."
          : "Login unavailable. Please try again later.",
      );
    } finally {
      (form.elements.namedItem("password") as HTMLInputElement).value = "";
      setBusy(false);
    }
  }
  return (
    <main className="login-layout">
      <section className="login-story" aria-label="About AgentPal">
        <Logo reversed />
        <div className="login-introduction">
          <span className="eyebrow">INTELLIGENT SHOPPING. HUMAN CONTROL.</span>
          <h1>
            Consider it.
            <br />
            <em>Carefully.</em>
          </h1>
          <p>
            Your agent finds the possibilities. Your rules set the boundaries.
            You stay in control.
          </p>
          <ol className="login-principles">
            <li>
              <span>01</span>
              <div>
                <strong>Discover with purpose</strong>
                <p>Product listings, matched to what you need.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Let your policy lead</strong>
                <p>Every proposal checked against your limits.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Keep the final say</strong>
                <p>Review the exceptions. Record your decision.</p>
              </div>
            </li>
          </ol>
        </div>
        <div className="login-note">
          <Icon name="shield" />
          <span>
            Independent PayPal Sandbox project.
            <br />
            Payment execution is disabled.
          </span>
        </div>
      </section>
      <section className="login-card">
        <div className="access-mark">
          <Icon name="lock" size={24} />
        </div>
        <span className="eyebrow">YOUR PRIVATE WORKSPACE</span>
        <h2>Welcome back.</h2>
        <p>Sign in to put your agent to work.</p>
        <form onSubmit={submit} aria-busy={busy}>
          <label>
            Owner login
            <input
              name="login"
              aria-describedby={error ? "login-error" : undefined}
              aria-invalid={Boolean(error)}
              autoComplete="username"
              required
              maxLength={128}
            />
          </label>
          <label>
            Password
            <input
              name="password"
              aria-describedby={error ? "login-error" : undefined}
              aria-invalid={Boolean(error)}
              type="password"
              autoComplete="current-password"
              required
              maxLength={128}
            />
          </label>
          <button className="button-primary" disabled={busy}>
            {busy ? (
              <>
                <span className="spinner" />
                Signing in…
              </>
            ) : (
              <>
                Sign in
                <Icon name="arrow" />
              </>
            )}
          </button>
        </form>
        {error && (
          <div id="login-error">
            <Message>{error}</Message>
          </div>
        )}
        <div className="login-security">
          <Icon name="shield" size={16} />
          <span>Owner access only. No public registration.</span>
        </div>
        <p className="login-disclosure">
          AgentPal is an independent application. It is not an official PayPal
          product.
        </p>
      </section>
    </main>
  );
}

function PolicyForm({
  policy,
  onSaved,
}: {
  policy: Policy | null;
  onSaved: () => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSaved(false);
    setBusy(true);
    const data = new FormData(event.currentTarget);
    try {
      await api("/policy", "PUT", {
        expected_policy_id: policy?.id ?? null,
        monthly_limit: data.get("monthly"),
        auto_approval_limit: data.get("auto"),
        hard_purchase_limit: data.get("hard"),
        purchase_ttl_seconds: Number(data.get("ttl")),
        merchant_allowlist: String(data.get("merchants"))
          .split(/[\n,]/)
          .map((x) => x.trim())
          .filter(Boolean),
      });
      await onSaved();
      setSaved(true);
    } catch (e) {
      const messages: Record<string, string> = {
        invalid_policy:
          "Check your amounts and lifetime. The auto-approval limit cannot exceed the hard purchase limit.",
        invalid_allowlist:
          "Use unique lowercase merchant domains without URLs or paths.",
        policy_conflict:
          "Your policy changed elsewhere. Refresh before saving again.",
        budget_conflict:
          "The budget cannot be lower than confirmed spending and outstanding reservations.",
      };
      setError(
        e instanceof ApiError && messages[e.code]
          ? messages[e.code]
          : "Policy could not be saved. Refresh your workspace before trying again.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel policy-panel">
      <div className="section-title">
        <div>
          <span className="eyebrow">YOUR FINANCIAL BOUNDARIES</span>
          <h2>Spending policy</h2>
        </div>
        <span className={`tag ${policy ? "tag-brand" : "tag-neutral"}`}>
          <Icon name={policy ? "shield" : "policy"} size={14} />
          {policy ? "Active policy" : "Setup required"}
        </span>
      </div>
      <p>
        Give your agent clear limits. Every proposal is evaluated against these
        rules, independently of the model.
      </p>
      <form
        className="policy-form"
        onSubmit={save}
        key={policy?.id ?? "new"}
        aria-busy={busy}
      >
        <label>
          Monthly budget ($)
          <input
            name="monthly"
            aria-label="Monthly budget ($)"
            aria-describedby="monthly-help"
            inputMode="decimal"
            pattern="[0-9]+(\.[0-9]{1,2})?"
            defaultValue={policy?.monthly_limit}
            required
          />
          <small id="monthly-help">
            Shared by all agents. Resets each calendar month.
          </small>
        </label>
        <label>
          Auto-approval up to ($)
          <input
            name="auto"
            aria-label="Auto-approval up to ($)"
            aria-describedby="auto-help"
            inputMode="decimal"
            pattern="[0-9]+(\.[0-9]{1,2})?"
            defaultValue={policy?.auto_approval_limit}
            required
          />
          <small id="auto-help">
            Above this amount, an eligible proposal needs your approval.
          </small>
        </label>
        <label>
          Hard purchase limit ($)
          <input
            name="hard"
            aria-label="Hard purchase limit ($)"
            aria-describedby="hard-help"
            inputMode="decimal"
            pattern="[0-9]+(\.[0-9]{1,2})?"
            defaultValue={policy?.hard_purchase_limit}
            required
          />
          <small id="hard-help">
            Proposals above this amount are rejected.
          </small>
        </label>
        <label>
          Proposal lifetime (seconds)
          <input
            name="ttl"
            aria-label="Proposal lifetime (seconds)"
            aria-describedby="ttl-help"
            type="number"
            min="1"
            step="1"
            defaultValue={policy?.purchase_ttl_seconds}
            required
          />
          <small id="ttl-help">Time to decide before a proposal expires.</small>
        </label>
        <label className="wide">
          Allowed merchant domains
          <textarea
            name="merchants"
            aria-label="Allowed merchant domains"
            aria-describedby="merchants-help"
            rows={3}
            placeholder="One canonical lowercase domain per line"
            defaultValue={policy?.merchant_allowlist.join("\n")}
            required
          />
          <small id="merchants-help">
            One canonical lowercase domain per line, or separated by commas. All
            other merchants are rejected.
          </small>
        </label>
        <div className="wide policy-notice">
          <Icon name="alert" size={18} />
          <p>
            Saving changes creates a new policy version and invalidates earlier
            proposals. No money moves. Budget resets at 00:00 UTC on the first;
            unused funds do not roll over.
          </p>
        </div>
        <div className="wide form-actions">
          <span>Applies to every new proposal.</span>
          <button disabled={busy}>
            {busy ? (
              <>
                <span className="spinner" />
                Saving…
              </>
            ) : (
              <>
                Save policy
                <Icon name="check" size={18} />
              </>
            )}
          </button>
        </div>
      </form>
      {error && <Message>{error}</Message>}
      {saved && (
        <Message tone="success">
          Policy saved. Your current rules are active.
        </Message>
      )}
    </section>
  );
}

function ProposalCard({
  item,
  onDecision,
}: {
  item: Purchase;
  onDecision: (item: Purchase, decision: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function decide(decision: string) {
    setBusy(true);
    setError("");
    try {
      await onDecision(item, decision);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Decision failed");
    } finally {
      setBusy(false);
    }
  }
  const state = proposalState(item);
  return (
    <article className={`proposal proposal-${state.tone}`} aria-busy={busy}>
      <div className="proposal-summary">
        <div className="product-glyph">
          <Icon name="package" size={24} />
        </div>
        <div className="proposal-title">
          <span className="merchant">{item.merchant_domain}</span>
          <h3>{item.title}</h3>
        </div>
        <span className={`tag tag-${state.tone}`}>
          <span className="status-dot" />
          {state.label}
        </span>
        <div className="proposal-price">
          {dollars(item.total_amount)}
          <small>
            USD · {item.quantity} × {dollars(item.unit_price)}
          </small>
        </div>
      </div>
      <div className="proposal-detail">
        <div className="rule-trace">
          <Icon name="shield" size={16} />
          <p>
            {[
              ...new Set([
                ...item.initial_reason_codes,
                ...item.eligibility.reason_codes,
              ]),
            ]
              .map(reasonLabel)
              .join(" · ")}
          </p>
        </div>
        <div className="proposal-meta">
          <span>
            <Icon name="clock" size={14} />
            Expires {new Date(item.expires_at).toLocaleString()}
          </span>
          <span>
            <Icon name="policy" size={14} />
            {item.policy_current ? "Current policy" : "Policy changed"}
          </span>
        </div>
      </div>
      <div className="proposal-footer">
        <span className="payment-note">
          <Icon name="lock" size={14} />
          Payment: {words(item.payment_status)} · execution disabled
        </span>
        {item.human_decision && (
          <span className="decision-record">
            <Icon
              name={
                item.human_decision.decision === "APPROVE" ? "check" : "close"
              }
              size={15}
            />
            Owner decision: {words(item.human_decision.decision)}
          </span>
        )}
      </div>
      {item.eligibility.can_approve || item.eligibility.can_deny ? (
        <div className="decision">
          <div>
            <strong>Your decision is required</strong>
            <p>
              Approve this exact item, quantity and amount. Consent only; no
              charge.
            </p>
          </div>
          <div className="decision-actions">
            <button
              disabled={busy || !item.eligibility.can_approve}
              onClick={() => decide("APPROVE")}
            >
              {busy ? "Recording…" : "Approve proposal"}
              <Icon name="check" size={16} />
            </button>
            <button
              className="secondary button-deny"
              disabled={busy || !item.eligibility.can_deny}
              onClick={() => decide("DENY")}
            >
              Deny
            </button>
          </div>
        </div>
      ) : null}
      {error && <Message>{error}</Message>}
    </article>
  );
}

function Workspace({
  owner,
  onLogout,
}: {
  owner: string;
  onLogout: () => void;
}) {
  const [tab, setTab] = useState("shopping");
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [budget, setBudget] = useState<Budget | null>(null);
  const [vault, setVault] = useState<VaultStatus | null>(null);
  const [shopping, setShopping] = useState<ShoppingStatus | null>(null);
  const [items, setItems] = useState<Purchase[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [result, setResult] = useState<ShoppingResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  async function refresh() {
    setRefreshing(true);
    const outcomes = await Promise.allSettled([
      api<Policy>("/policy"),
      api<Budget>("/budget/current"),
      api<VaultStatus>("/paypal/vault/status"),
      api<ShoppingStatus>("/shopping/status"),
      api<{ items: Purchase[]; has_more: boolean }>("/purchases?limit=20"),
    ]);
    if (
      outcomes.some(
        (x) =>
          x.status === "rejected" &&
          x.reason instanceof ApiError &&
          x.reason.status === 401,
      )
    ) {
      onLogout();
      return;
    }
    const [p, b, v, s, history] = outcomes;
    if (p.status === "fulfilled") setPolicy(p.value);
    else if (
      p.reason instanceof ApiError &&
      p.reason.code === "policy_setup_required"
    )
      setPolicy(null);
    if (b.status === "fulfilled") setBudget(b.value);
    if (v.status === "fulfilled") setVault(v.value);
    if (s.status === "fulfilled") setShopping(s.value);
    if (history.status === "fulfilled") {
      setItems(history.value.items);
      setHasMore(history.value.has_more);
    }
    if (
      outcomes.some(
        (x) =>
          x.status === "rejected" &&
          !(
            x.reason instanceof ApiError &&
            x.reason.code === "policy_setup_required"
          ),
      )
    ) {
      setNotice(
        "Some workspace data could not be loaded. Refresh to check current state.",
      );
    } else setNotice("");
    setRefreshing(false);
  }
  useEffect(() => {
    void refresh().finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    document.getElementById("main-content")?.scrollTo({ top: 0 });
    window.scrollTo({ top: 0 });
  }, [tab]);

  async function run(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setResult(null);
    const request = String(new FormData(event.currentTarget).get("request"));
    try {
      setResult(
        await api<ShoppingResult>("/shopping/runs", "POST", { request }),
      );
    } catch (e) {
      setError(
        `${e instanceof Error ? e.message : "Run failed"}. No automatic retry. Check proposal history before trying again.`,
      );
    } finally {
      await refresh();
      setBusy(false);
    }
  }

  async function decide(item: Purchase, decision: string) {
    try {
      await api(`/purchases/${item.id}/approval`, "PUT", {
        decision,
        snapshot_hash: item.snapshot_hash,
        snapshot_hash_version: item.snapshot_hash_version,
      });
    } finally {
      await refresh();
    }
  }

  async function logout() {
    try {
      await api("/auth/logout", "POST", {});
      onLogout();
    } catch {
      setError("Logout failed. Your session may still be active.");
    }
  }
  async function loadMore() {
    try {
      const page = await api<{ items: Purchase[]; has_more: boolean }>(
        `/purchases?limit=20&offset=${items.length}`,
      );
      setItems((previous) => [
        ...previous,
        ...page.items.filter((x) => !previous.some((p) => p.id === x.id)),
      ]);
      setHasMore(page.has_more);
    } catch {
      setError("Could not load more proposals.");
    }
  }
  const waiting = items.filter((x) => x.eligibility.can_approve).length;
  const offline = shopping?.mode === "offline_rehearsal";
  const connectionVerified =
    vault?.connected && vault.agreement_state === "verified";
  const connectionLabel = !vault
    ? "Status unavailable"
    : connectionVerified
      ? "Agreement verified"
      : vault.connected
        ? "Verification needed"
        : "Not connected";
  const resetDate = budget
    ? new Date(budget.next_reset_at).toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
        timeZone: "UTC",
      })
    : null;
  return (
    <div className="workspace">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <aside className="sidebar">
        <div className="sidebar-brand">
          <Logo />
          <span className="environment-label">Sandbox</span>
        </div>
        <div className="workspace-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {NAVIGATION.map(({ key, icon, label }) => (
            <button
              key={key}
              className={tab === key ? "active" : ""}
              onClick={() => setTab(key)}
              aria-current={tab === key ? "page" : undefined}
            >
              <Icon name={icon} />
              <span>{label}</span>
              {key === "proposals" && waiting > 0 && (
                <b
                  aria-label={`${waiting} proposals awaiting approval on this page`}
                >
                  {waiting}
                </b>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-assurance">
          <Icon name="shield" size={22} />
          <strong>Your rules. Always.</strong>
          <p>
            Your agent proposes.
            <br />
            Your policy decides.
          </p>
          <span>Payments are disabled</span>
        </div>
        <div className="sidebar-bottom">
          <span className="avatar" aria-hidden="true">
            {owner[0].toUpperCase()}
          </span>
          <div>
            <strong>{owner}</strong>
            <small>Human Owner</small>
          </div>
          <button
            className="icon-button"
            aria-label="Sign out"
            onClick={logout}
          >
            <Icon name="logout" size={18} />
          </button>
        </div>
      </aside>
      <main className="content" id="main-content" tabIndex={-1}>
        <div className="topline">
          <span>
            Owner workspace <Icon name="chevron" size={12} />
            {NAVIGATION.find((x) => x.key === tab)?.label}
          </span>
          <span>
            <Icon name="shield" size={14} />
            Sandbox environment
          </span>
        </div>
        <header className="page-header">
          <div>
            <h1>
              {tab === "policy"
                ? "Your spending rules"
                : tab === "connection"
                  ? "PayPal connection"
                  : tab === "proposals"
                    ? "Purchase proposals"
                    : "Shopping agent"}
            </h1>
            <p>
              {tab === "policy"
                ? "Set clear boundaries. Keep every proposal accountable."
                : tab === "connection"
                  ? "A clear view of your saved method and agreement."
                  : tab === "proposals"
                    ? "Every proposal, policy check and human decision."
                    : "A little intelligence. A lot of control."}
            </p>
          </div>
          <button
            className="secondary compact"
            disabled={busy || refreshing}
            onClick={() => void refresh()}
          >
            <Icon name="refresh" size={16} />
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </header>
        <div className={`banner ${offline ? "banner-rehearsal" : ""}`}>
          <Icon name={offline ? "alert" : "lock"} size={19} />
          <div>
            <strong>
              {offline ? "Offline rehearsal" : "Payment execution is disabled"}
            </strong>
            <p>
              {offline
                ? "Scripted model · synthetic product listings · disposable database. No external calls or payments."
                : "Proposals and Owner decisions do not move money. No Orders, Captures or charges are available."}
            </p>
          </div>
          <span className="banner-label">
            {offline ? "SIMULATED INPUTS" : "PROPOSE ONLY"}
          </span>
        </div>
        {notice && <Message tone="warning">{notice}</Message>}
        {error && <Message>{error}</Message>}

        <section
          className="metrics"
          aria-label="Workspace summary"
          aria-busy={loading}
        >
          <div className="metric-budget">
            <div className="metric-label">
              <span>Available this month</span>
              <Icon name="policy" size={18} />
            </div>
            <strong className="metric-amount">
              {dollars(budget?.available_amount)}
              <span>{budget ? "USD" : ""}</span>
            </strong>
            <div className="budget-detail">
              {budget ? (
                <>
                  <span>
                    Spent <b>{dollars(budget.spent_amount)}</b>
                  </span>
                  <span>
                    Reserved <b>{dollars(budget.reserved_amount)}</b>
                  </span>
                  <span>
                    of <b>{dollars(budget.limit_amount)}</b>
                  </span>
                </>
              ) : (
                <span>
                  {loading
                    ? "Loading your budget…"
                    : "Set a policy to establish your budget."}
                </span>
              )}
            </div>
          </div>
          <div>
            <div className="metric-label">
              <span>Awaiting your decision</span>
              <Icon name="proposals" size={18} />
            </div>
            <strong className="metric-amount">
              {loading ? "—" : waiting}
              <span>{waiting === 1 ? "proposal" : "proposals"}</span>
            </strong>
            <small>On this page · approval records consent only</small>
          </div>
          <div className="metric-connection">
            <div className="metric-label">
              <span>PayPal Sandbox</span>
              <Icon name="connection" size={18} />
            </div>
            <strong className="connection-text">{connectionLabel}</strong>
            <button
              className="text-button"
              onClick={() => setTab("connection")}
            >
              View connection
              <Icon name="arrow" size={15} />
            </button>
          </div>
        </section>
        <div className="accounting-note">
          <Icon name="clock" size={13} />
          UTC calendar month
          {resetDate
            ? ` · Next reset ${resetDate}, 00:00 UTC`
            : " · No rollover"}
        </div>

        {tab === "policy" && <PolicyForm policy={policy} onSaved={refresh} />}
        {tab === "connection" && (
          <section className="connection-layout">
            <div className="panel connection-panel">
              <div className="section-title">
                <div>
                  <span className="eyebrow">PAYMENT CONNECTION</span>
                  <h2>PayPal Sandbox</h2>
                </div>
                <span
                  className={`tag ${connectionVerified ? "tag-success" : "tag-warning"}`}
                >
                  <span className="status-dot" />
                  {connectionLabel}
                </span>
              </div>
              <p>
                Saved payment methods and verified consent are separate
                requirements. A browser return alone does not confirm either.
              </p>
              <dl className="connection-details">
                <div>
                  <dt>Saved payment method</dt>
                  <dd>
                    {vault
                      ? vault.connected
                        ? "Connected"
                        : "Not connected"
                      : "Unavailable"}
                  </dd>
                </div>
                <div>
                  <dt>Agreement verification</dt>
                  <dd>
                    {vault
                      ? words(vault.agreement_state ?? "unverified")
                      : "Unavailable"}
                  </dd>
                </div>
                <div>
                  <dt>Latest enrollment</dt>
                  <dd>
                    {vault
                      ? words(vault.onboarding?.state ?? "none")
                      : "Unavailable"}
                  </dd>
                </div>
                <div>
                  <dt>Payment execution</dt>
                  <dd>
                    <Icon name="lock" size={14} />
                    Disabled
                  </dd>
                </div>
              </dl>
              <div className="connection-notice">
                <Icon name="alert" size={19} />
                <div>
                  <strong>Enrollment is paused</strong>
                  <p>
                    PayPal-hosted consent encountered a merchant/payer account
                    conflict. This workspace does not start enrollment or call
                    payment APIs.
                  </p>
                </div>
              </div>
            </div>
            <div className="navy-panel">
              <Icon name="shield" size={30} />
              <h2>
                Consent is just
                <br />
                the beginning.
              </h2>
              <p>
                A verified Vault agreement alone does not authorize an
                individual purchase or enable payment execution.
              </p>
              <ul>
                <li>
                  <Icon name="check" size={16} />
                  Your spending policy still applies
                </li>
                <li>
                  <Icon name="check" size={16} />
                  Required human approval stays separate
                </li>
                <li>
                  <Icon name="lock" size={16} />
                  Payment execution remains disabled
                </li>
              </ul>
              <small>
                AgentPal is an independent application, not an official PayPal
                product.
              </small>
            </div>
          </section>
        )}

        {tab === "shopping" && (
          <>
            <section className="composer panel">
              <div className="composer-heading">
                <div>
                  <span className="eyebrow">START WITH WHAT YOU NEED</span>
                  <h2>
                    Your next find.
                    <br />
                    Within your rules.
                  </h2>
                </div>
                <span className="tag tag-outline">
                  <Icon name="agent" size={16} />
                  Proposals only
                </span>
              </div>
              <div className="composer-body">
                <div className="request-panel">
                  <form onSubmit={run} aria-busy={busy}>
                    <label htmlFor="shopping-request">
                      Describe what you need
                    </label>
                    <textarea
                      id="shopping-request"
                      name="request"
                      rows={3}
                      placeholder="Find a wireless keyboard for my desk…"
                      maxLength={1000}
                      required
                      aria-describedby="request-privacy"
                    />
                    <div className="request-footer">
                      <span>
                        <Icon name="shield" size={15} />
                        Your policy is checked independently
                      </span>
                      <button disabled={busy || !shopping?.ready}>
                        {busy ? (
                          <>
                            <span className="spinner" />
                            Finding & evaluating…
                          </>
                        ) : (
                          <>
                            Find & propose
                            <Icon name="arrow" size={18} />
                          </>
                        )}
                      </button>
                    </div>
                  </form>
                  <p className="privacy-note" id="request-privacy">
                    <Icon name="lock" size={13} />
                    No passwords or payment details. Live requests go to the
                    configured model and product source.
                  </p>
                </div>
                <div className="guardrail-summary">
                  <div className="guardrail-title">
                    <Icon name="policy" size={18} />
                    <strong>Active guardrails</strong>
                  </div>
                  <dl>
                    <div>
                      <dt>Auto-approval up to</dt>
                      <dd>{dollars(policy?.auto_approval_limit)}</dd>
                    </div>
                    <div>
                      <dt>Per-purchase limit</dt>
                      <dd>{dollars(policy?.hard_purchase_limit)}</dd>
                    </div>
                    <div>
                      <dt>Merchant allowlist</dt>
                      <dd>
                        {policy
                          ? `${policy.merchant_allowlist.length} ${policy.merchant_allowlist.length === 1 ? "domain" : "domains"}`
                          : "Not configured"}
                      </dd>
                    </div>
                  </dl>
                  <button
                    className="text-button"
                    onClick={() => setTab("policy")}
                  >
                    Manage policy
                    <Icon name="arrow" size={15} />
                  </button>
                </div>
              </div>
              {shopping && !shopping.ready && (
                <div className="composer-setup">
                  <Message tone="warning">
                    <strong>A little setup first.</strong>{" "}
                    {!shopping.policy_ready ? "Set your spending policy. " : ""}
                    {!shopping.agent_ready
                      ? "Shopping agent registration is required. "
                      : ""}
                    {!shopping.model_ready
                      ? "Server model configuration is required. "
                      : ""}
                    {!shopping.discovery_ready
                      ? "Product discovery configuration is required."
                      : ""}
                  </Message>
                </div>
              )}
              <ol className="workflow-strip" aria-label="Shopping workflow">
                {[
                  "Discover",
                  "Verify offer",
                  "Evaluate policy",
                  "You decide",
                ].map((label, index) => (
                  <li key={label}>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    {label}
                    {index < 3 && <Icon name="chevron" size={13} />}
                  </li>
                ))}
              </ol>
            </section>
            {busy && (
              <div className="working" role="status">
                <span className="spinner" />
                <div>
                  <strong>Finding a proposal that fits.</strong>
                  <p>
                    Discovering products and checking your policy. No payment
                    will be made.
                  </p>
                </div>
              </div>
            )}
            {result && (
              <section className="panel run-receipt">
                <div className="section-title">
                  <div>
                    <span className="eyebrow">YOUR RUN, EXPLAINED</span>
                    <h2>
                      {result.purchase
                        ? "From intent to proposal."
                        : "No suitable offer selected"}
                    </h2>
                  </div>
                  <span className="tag tag-neutral">
                    {result.candidates.length}{" "}
                    {result.candidates.length === 1 ? "offer" : "offers"} found
                  </span>
                </div>
                <ol className="steps">
                  {result.steps.map((step) => (
                    <li key={step}>
                      <Icon name="check" size={15} />
                      {step}
                    </li>
                  ))}
                </ol>
                <div className="search-receipt">
                  <span>
                    Search: <strong>{result.query}</strong>
                  </span>
                  <span>
                    Quantity: <strong>{result.quantity}</strong>
                  </span>
                </div>
                <div className="candidate-list">
                  {result.candidates.map((candidate, index) => (
                    <div
                      className="candidate"
                      key={`${candidate.product_id}-${index}`}
                    >
                      <span className="product-glyph">
                        <Icon name="package" size={20} />
                      </span>
                      <div>
                        <strong>{candidate.title}</strong>
                        <small>
                          {candidate.merchant_domain} ·{" "}
                          {words(candidate.availability)}
                        </small>
                      </div>
                      <span className="candidate-price">
                        {dollars(candidate.unit_price)}
                        <small>USD / item</small>
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </>
        )}

        {(tab === "shopping" || tab === "proposals") && (
          <section aria-labelledby="proposals-heading">
            <div className="section-title proposal-heading">
              <div>
                <span className="eyebrow">YOUR DECISION TRAIL</span>
                <h2 id="proposals-heading">
                  {tab === "proposals" ? "All proposals" : "Recent proposals"}
                </h2>
              </div>
              <span className="count-label">
                {loading ? "Loading…" : `${items.length} shown`}
              </span>
            </div>
            {loading ? (
              <div className="empty" role="status">
                <span className="spinner" />
                <p>Loading your proposal history…</p>
              </div>
            ) : items.length === 0 ? (
              <div className="empty">
                <div className="empty-symbol">
                  <Icon name="proposals" size={28} />
                </div>
                <h3>A considered purchase starts here.</h3>
                <p>
                  Ask your agent to find a product.
                  <br />
                  Its proposal and policy checks will appear here.
                </p>
                {tab === "proposals" && (
                  <button
                    className="secondary"
                    onClick={() => setTab("shopping")}
                  >
                    Start a shopping request
                    <Icon name="arrow" size={17} />
                  </button>
                )}
              </div>
            ) : (
              <div className="proposal-list">
                {items.map((item) => (
                  <ProposalCard key={item.id} item={item} onDecision={decide} />
                ))}
              </div>
            )}
            {hasMore && (
              <button
                className="secondary load-more"
                onClick={() => void loadMore()}
              >
                Load more proposals
                <Icon name="chevron" size={16} />
              </button>
            )}
          </section>
        )}
        <footer>
          <Logo compact />
          <p>
            Independent PayPal Sandbox project. No payment execution.
            <br />
            External listings are not retailer checkout quotes. No retailer
            fulfillment.
          </p>
          <span>Built around your rules.</span>
        </footer>
      </main>
    </div>
  );
}

function App() {
  const [owner, setOwner] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    api<{ login_name: string }>("/auth/me")
      .then((x) => setOwner(x.login_name))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);
  if (loading)
    return (
      <main className="loading" role="status">
        Opening AgentPal…
      </main>
    );
  return owner ? (
    <Workspace owner={owner} onLogout={() => setOwner(null)} />
  ) : (
    <Login onLogin={setOwner} />
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
