# AgentPal frontend design

The existing React application now uses the supplied PayPal-inspired palette,
with an original AgentPal identity. This is an independent Sandbox application,
not an official PayPal product. The redesign changes presentation and feedback;
it does not introduce backend features, alter API contracts or enable payments.

## Design system

- `src/style.css` contains the exact supplied color, typography, spacing and
  radius tokens. White is the page base, cobalt is the primary action/navigation
  color, sky blue identifies the shopping composer, and ice blue supports selected
  states. Semantic success, warning and error colors are separate and tested.
- Inter is self-hosted using `@fontsource-variable/inter`. The bundled font and
  its SIL Open Font License load locally; no external font service is contacted.
- SVG icons replace navigation glyphs. Text labels accompany financial states;
  status is never conveyed through color alone. Focus rings, a skip link,
  descriptive form labels and reduced-motion styles are included.
- Mobile uses a dedicated header, bottom navigation and an independently
  scrolling content area. Navigation reserves its own space and cannot obscure
  approval controls. Changing sections resets the content scroll position.

## Logo assets

All files are editable SVG paths. The wordmark is outlined from the licensed
Inter font, so SVG rendering does not depend on a separately installed font.
The geometric A/forward-bar symbol is original; no PayPal brand assets or
double-P monogram are included.

| Asset | Purpose |
|---|---|
| `src/assets/brand/agentpal-primary.svg` | Navy/sky symbol and navy wordmark for light surfaces |
| `src/assets/brand/agentpal-reversed.svg` | White/sky symbol and white wordmark for navy surfaces |
| `src/assets/brand/agentpal-symbol.svg` | Compact navy/sky mark, matching the sidebar lockup |
| `src/icon.svg` | Navy-backed favicon |
| `public/assets/Inter-LICENSE.txt` | Font license, also included in the built assets |

## Financial-state presentation

Backend eligibility controls the displayed proposal state. An approval means
“Approved by you”; automatic authorization means “Auto-authorized.” Neither
means paid or completed. Expired, policy-invalidated, denied, rejected and unknown
states cannot inherit a success appearance. The separately reported backend
payment status stays visible, and execution remains disabled.

Amounts are formatted directly from decimal strings, with grouped integer
digits and at least two fractional digits. Values are never converted through
floating point or rounded; higher-precision source strings remain exact. Missing
values display a dash. Budget, spent, reserved and history counts come from the
existing APIs. Counts explicitly describe the currently loaded page.

The shopping screen is a request composer and result receipt, reflecting the
existing one-request workflow. It does not invent chat history, a payment
timeline, checkout, account balances or connection capabilities. Offline mode
continues to identify its scripted model, synthetic listings and disposable DB.

## Exact changed-file manifest for this redesign

Pre-existing backend and project changes were preserved. The redesign changes
only these frontend files:

| File | Change |
|---|---|
| `src/main.tsx` | Existing login, workspace, policy, proposal and connection markup; SVG navigation; loading/error/success feedback; mobile scroll reset |
| `src/style.css` | Central theme tokens, self-hosted font face, all component styling, responsive shell, focus and reduced-motion rules |
| `src/ui.tsx` | Reusable SVG icons, AgentPal logo variants and accessible messages |
| `src/presentation.ts` | Exact decimal display, conservative proposal labels and readable policy reasons |
| `src/vite-env.d.ts` | Vite SVG asset typing |
| `src/assets/brand/agentpal-primary.svg` | New outlined primary logo |
| `src/assets/brand/agentpal-reversed.svg` | New outlined reversed logo |
| `src/assets/brand/agentpal-symbol.svg` | New compact symbol |
| `src/icon.svg` | Replaced prototype favicon |
| `index.html` | Navy browser theme and independent-product description |
| `package.json` | Local font dependency and frontend test command |
| `package-lock.json` | Locked local font dependency |
| `public/assets/Inter-LICENSE.txt` | Font attribution/license |
| `tests/presentation.test.ts` | Financial-label, decimal precision, exact palette and contrast regressions |
| `DESIGN.md` | This file-by-file delivery and verification report |

`src/api.ts`, Python code, migrations, financial execution logic, private config,
credentials, persistent records and infrastructure are unchanged by this task.
Generated Vite assets and sanitized screenshots are ignored build/test outputs.
The temporary accessibility-checker script is removed from built assets after QA.

## Verification — October 23, 2026

Run the reproducible checks from the repository root:

```sh
rtk proxy npm --prefix frontend test
rtk proxy npm --prefix frontend run build
rtk proxy uv run pytest tests/test_shopping.py tests/test_demo.py tests/test_approvals.py tests/test_policy.py tests/test_auth.py -q
```

- Five frontend tests pass, including unknown/blocked states, exact large decimal
  values, the supplied palette, and WCAG AA text/control contrast combinations.
- 189 relevant Python regressions pass using disposable PostgreSQL and mocked
  providers. No runtime credentials or persistent database are used.
- Real Chromium interaction checks use only the existing isolated rehearsal:
  login/logout, empty history, shopping loading/disabled state, source results,
  human approval, denial, automatic authorization, policy rejection, durable
  history reload, invalid policy feedback, policy save/success and invalidation
  of earlier proposals, read-only unverified PayPal status, and mobile navigation.
- Layouts are inspected at desktop 1440px, tablet 768px, mobile 390px and narrow
  mobile 320px. Financial controls retain readable, separate states and there is
  no horizontal overflow. Additional error/setup states are tested with browser
  response interception, without creating backend features or financial records.
- Automated rendered WCAG A/AA checks cover login, shopping, policies, proposals
  and connection screens. They complement manual visual/keyboard checks and are
  not a claim of formal accessibility certification.

Sanitized previews are saved under `output/playwright/agentpal-premium-*.png`,
including desktop login/shopping/proposals/policy/connection, mobile screens,
loading and policy error feedback. All displayed financial data are explicitly
labeled rehearsal fixtures. No real AI, PayPal enrollment or payment was run.
