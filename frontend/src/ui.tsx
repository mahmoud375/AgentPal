import type { ReactNode } from "react";
import primaryLogo from "./assets/brand/agentpal-primary.svg";
import reversedLogo from "./assets/brand/agentpal-reversed.svg";
import compactLogo from "./assets/brand/agentpal-symbol.svg";

export type IconName =
  | "agent"
  | "proposals"
  | "policy"
  | "connection"
  | "arrow"
  | "refresh"
  | "logout"
  | "shield"
  | "lock"
  | "check"
  | "alert"
  | "package"
  | "clock"
  | "close"
  | "chevron";

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    agent: (
      <>
        <path d="m4 19 6-14 4 9 6 2-6 4-4-6z" />
        <path d="m17 4 .5 2.5L20 7l-2.5.5L17 10l-.5-2.5L14 7l2.5-.5z" />
      </>
    ),
    proposals: (
      <>
        <rect x="5" y="3" width="14" height="18" rx="2" />
        <path d="M9 8h6M9 12h6M9 16h4" />
      </>
    ),
    policy: (
      <>
        <path d="M4 6h16M4 12h16M4 18h16" />
        <circle cx="8" cy="6" r="2" />
        <circle cx="16" cy="12" r="2" />
        <circle cx="10" cy="18" r="2" />
      </>
    ),
    connection: (
      <>
        <rect x="3" y="5" width="18" height="14" rx="3" />
        <path d="M3 10h18M7 15h4" />
      </>
    ),
    arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6.5 6.5A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.5 5.5" />
      </>
    ),
    logout: (
      <>
        <path d="M9 4H4v16h5M10 12h11m-4-4 4 4-4 4" />
      </>
    ),
    shield: (
      <>
        <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z" />
        <path d="m8.5 12 2.5 2.5 4.5-5" />
      </>
    ),
    lock: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="2" />
        <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    alert: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v6M12 16.5h.01" />
      </>
    ),
    package: (
      <>
        <path d="m12 3 9 5v9l-9 5-9-5V8zM3 8l9 5 9-5M12 13v9M7.5 5.5l9 5" />
      </>
    ),
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    close: <path d="m6 6 12 12M18 6 6 18" />,
    chevron: <path d="m9 5 7 7-7 7" />,
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths[name]}
    </svg>
  );
}

export function Logo({
  reversed = false,
  compact = false,
}: {
  reversed?: boolean;
  compact?: boolean;
}) {
  return (
    <span className={`brand ${compact ? "brand-compact" : ""}`}>
      <img
        src={compact ? compactLogo : reversed ? reversedLogo : primaryLogo}
        width={compact ? 40 : 166}
        height={40}
        alt="AgentPal"
      />
    </span>
  );
}

export function Message({
  children,
  tone = "error",
}: {
  children: ReactNode;
  tone?: "error" | "warning" | "success";
}) {
  return (
    <div
      className={`message message-${tone}`}
      role={tone === "error" ? "alert" : "status"}
    >
      <Icon name={tone === "success" ? "check" : "alert"} size={18} />
      <div>{children}</div>
    </div>
  );
}
