import { env } from "@/env";

function getBaseOrigin() {
  if (typeof window !== "undefined") {
    return window.location.origin;
  }
  // Fallback for SSR
  return "http://localhost:2026";
}

export function getBackendBaseURL() {
  if (env.NEXT_PUBLIC_BACKEND_BASE_URL) {
    return new URL(env.NEXT_PUBLIC_BACKEND_BASE_URL, getBaseOrigin())
      .toString()
      .replace(/\/+$/, "");
  } else {
    return "";
  }
}

export function getLangGraphBaseURL(isMock?: boolean) {
  if (env.NEXT_PUBLIC_LANGGRAPH_BASE_URL) {
    return new URL(
      env.NEXT_PUBLIC_LANGGRAPH_BASE_URL,
      getBaseOrigin(),
    ).toString();
  } else if (isMock) {
    if (typeof window !== "undefined") {
      return `${window.location.origin}/mock/api`;
    }
    return "http://localhost:3000/mock/api";
  } else {
    // In local nginx-on-2026 development, use the gateway-backed compat API so
    // thread creation and stream/history reads share the same backend state.
    if (typeof window !== "undefined") {
      if (window.location.port === "2026") {
        return `${window.location.origin}/api/langgraph-compat`;
      }
      return `${window.location.origin}/api/langgraph`;
    }
    // Fallback for SSR/local dev
    return "http://localhost:2026/api/langgraph-compat";
  }
}
