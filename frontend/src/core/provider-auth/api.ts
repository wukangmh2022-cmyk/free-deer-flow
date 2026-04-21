import { getBackendBaseURL } from "../config";

export type ProviderKey = "deepseek" | "xiaomi";

export type ProviderAuthStatus = {
  provider: ProviderKey;
  label: string;
  model: string;
  ready: boolean;
  has_session_state: boolean;
  has_cookie_store: boolean;
  session_state_path: string;
  profile_dir: string;
};

export type ProviderAuthStatusResponse = {
  hasAnyReady: boolean;
  providers: Record<ProviderKey, ProviderAuthStatus>;
};

export async function loadProviderAuthStatus() {
  const response = await fetch(`${getBackendBaseURL()}/api/provider-auth/status`, {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`Failed to load provider status (${response.status})`);
  }
  return (await response.json()) as ProviderAuthStatusResponse;
}

export async function openProviderLogin(provider: ProviderKey) {
  const response = await fetch(`${getBackendBaseURL()}/api/provider-auth/open-login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ provider }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail =
      payload && typeof payload.detail === "string"
        ? payload.detail
        : `Failed to open ${provider} login`;
    throw new Error(detail);
  }

  return payload as {
    provider: ProviderKey;
    label: string;
    model: string;
    url?: string;
    headless?: boolean;
    profile_dir?: string;
  };
}
