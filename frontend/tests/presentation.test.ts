import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dollars, proposalState } from "../src/presentation.ts";

const purchase = (status: string, overrides = {}) => ({
  eligibility: {
    status,
    can_approve: false,
    can_deny: false,
    reason_codes: [],
    execution_ready: false as const,
  },
  initial_decision: "AWAITING_HUMAN_APPROVAL",
  expired: false,
  policy_current: true,
  ...overrides,
});

test("approval and auto-authorization never claim a completed payment", () => {
  for (const status of [
    "APPROVED_PENDING_DISPATCH_CHECKS",
    "AUTO_AUTHORIZED_PENDING_DISPATCH_CHECKS",
  ]) {
    const state = proposalState(purchase(status));
    assert.doesNotMatch(state.label, /paid|completed|captured|success/i);
  }
  assert.equal(
    proposalState(purchase("APPROVED_PENDING_DISPATCH_CHECKS")).label,
    "Approved by you",
  );
});

test("blocked or unknown backend states cannot inherit a success appearance", () => {
  assert.equal(
    proposalState(purchase("BLOCKED", { initial_decision: "REJECTED" })).label,
    "Rejected",
  );
  assert.equal(
    proposalState(purchase("BLOCKED", { expired: true })).label,
    "Expired",
  );
  assert.equal(
    proposalState(purchase("BLOCKED", { policy_current: false })).label,
    "Policy changed",
  );
  assert.equal(proposalState(purchase("DENIED")).tone, "danger");
  assert.equal(proposalState(purchase("NEW_UNKNOWN_STATE")).tone, "warning");
});

test("financial values preserve backend decimal precision and absent values", () => {
  assert.equal(
    dollars("123456789012345678901234567890.12"),
    "$123,456,789,012,345,678,901,234,567,890.12",
  );
  assert.equal(dollars("-24.00"), "-$24.00");
  assert.equal(dollars("0"), "$0.00");
  assert.equal(dollars("1.5"), "$1.50");
  assert.equal(dollars("0.001"), "$0.001");
  assert.equal(dollars(undefined), "—");
  assert.equal(dollars("NaN"), "Unavailable");
});

const css = readFileSync(new URL("../src/style.css", import.meta.url), "utf8");
const token = (name: string) => {
  const value = css.match(new RegExp(`${name}:\\s*(#[a-f0-9]{6})`, "i"))?.[1];
  assert.ok(value, `Missing token ${name}`);
  return value;
};
function luminance(hex: string) {
  const c = [1, 3, 5]
    .map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((x) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
  return c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
}
function contrast(a: string, b: string) {
  const x = luminance(a),
    y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

test("the supplied brand palette is exact", () => {
  for (const [name, hex] of Object.entries({
    "--color-cerulean-surge": "#60CDFF",
    "--color-cobalt-vault": "#002991",
    "--color-ice-mist": "#B8E9FF",
    "--color-midnight": "#000000",
    "--color-snow": "#FFFFFF",
    "--color-parchment": "#F1EFEA",
    "--color-ash": "#B3B3B3",
    "--color-fog": "#CCCCCC",
  }))
    assert.equal(token(name).toUpperCase(), hex);
});

test("brand, body, disabled and semantic text combinations meet WCAG AA", () => {
  for (const [a, b] of [
    ["--color-snow", "--color-cobalt-vault"],
    ["--color-cobalt-vault", "--color-cerulean-surge"],
    ["--color-cobalt-vault", "--color-ice-mist"],
    ["--text-secondary", "--color-snow"],
    ["--text-secondary", "--color-parchment"],
    ["--success-text", "--success-surface"],
    ["--warning-text", "--warning-surface"],
    ["--danger-text", "--danger-surface"],
  ])
    assert.ok(contrast(token(a), token(b)) >= 4.5, `${a} on ${b}`);
  assert.ok(contrast("#363636", token("--color-ash")) >= 4.5);
  assert.ok(contrast(token("--control-border"), token("--color-snow")) >= 3);
});
