import type { AgentThreadContext } from "../threads";

export const DEFAULT_LOCAL_SETTINGS: LocalSettings = {
  notification: {
    enabled: true,
  },
  context: {
    context_compression_enabled: false,
    model_name: undefined,
    mode: undefined,
    reasoning_effort: undefined,
  },
};

export const LOCAL_SETTINGS_KEY = "deerflow.local-settings";
export const THREAD_MODEL_KEY_PREFIX = "deerflow.thread-model.";
export const THREAD_CONTEXT_COMPRESSION_KEY_PREFIX =
  "deerflow.thread-context-compression.";

function isBrowser(): boolean {
  return typeof window !== "undefined";
}

export interface LocalSettings {
  notification: {
    enabled: boolean;
  };
  context: Omit<
    AgentThreadContext,
    | "thread_id"
    | "is_plan_mode"
    | "thinking_enabled"
    | "subagent_enabled"
    | "model_name"
    | "reasoning_effort"
  > & {
    model_name?: string | undefined;
    mode: "flash" | "thinking" | "pro" | "ultra" | undefined;
    reasoning_effort?: "minimal" | "low" | "medium" | "high";
  };
}

function mergeLocalSettings(settings?: Partial<LocalSettings>): LocalSettings {
  return {
    ...DEFAULT_LOCAL_SETTINGS,
    context: {
      ...DEFAULT_LOCAL_SETTINGS.context,
      ...settings?.context,
    },
    notification: {
      ...DEFAULT_LOCAL_SETTINGS.notification,
      ...settings?.notification,
    },
  };
}

function getThreadModelStorageKey(threadId: string): string {
  return `${THREAD_MODEL_KEY_PREFIX}${threadId}`;
}

function getThreadCompressionStorageKey(threadId: string): string {
  return `${THREAD_CONTEXT_COMPRESSION_KEY_PREFIX}${threadId}`;
}

export function getThreadModelName(threadId: string): string | undefined {
  if (!isBrowser()) {
    return undefined;
  }
  return localStorage.getItem(getThreadModelStorageKey(threadId)) ?? undefined;
}

export function saveThreadModelName(
  threadId: string,
  modelName: string | undefined,
) {
  if (!isBrowser()) {
    return;
  }
  const key = getThreadModelStorageKey(threadId);
  if (!modelName) {
    localStorage.removeItem(key);
    return;
  }
  localStorage.setItem(key, modelName);
}

export function getThreadContextCompressionEnabled(
  threadId: string,
): boolean | undefined {
  if (!isBrowser()) {
    return undefined;
  }
  const value = localStorage.getItem(getThreadCompressionStorageKey(threadId));
  if (value === null) {
    return undefined;
  }
  return value === "true";
}

export function saveThreadContextCompressionEnabled(
  threadId: string,
  enabled: boolean | undefined,
) {
  if (!isBrowser()) {
    return;
  }
  const key = getThreadCompressionStorageKey(threadId);
  if (enabled === undefined) {
    localStorage.removeItem(key);
    return;
  }
  localStorage.setItem(key, String(enabled));
}

export function applyThreadContextOverrides(
  settings: LocalSettings,
  overrides: {
    model_name?: string | undefined;
    context_compression_enabled?: boolean | undefined;
  },
): LocalSettings {
  if (
    overrides.model_name === undefined &&
    overrides.context_compression_enabled === undefined
  ) {
    return settings;
  }
  return {
    ...settings,
    context: {
      ...settings.context,
      ...(overrides.model_name !== undefined
        ? { model_name: overrides.model_name }
        : {}),
      ...(overrides.context_compression_enabled !== undefined
        ? {
            context_compression_enabled:
              overrides.context_compression_enabled,
          }
        : {}),
    },
  };
}

export function getLocalSettings(): LocalSettings {
  if (!isBrowser()) {
    return DEFAULT_LOCAL_SETTINGS;
  }
  const json = localStorage.getItem(LOCAL_SETTINGS_KEY);
  try {
    if (json) {
      const settings = JSON.parse(json) as Partial<LocalSettings>;
      return mergeLocalSettings(settings);
    }
  } catch {}
  return DEFAULT_LOCAL_SETTINGS;
}

export function saveLocalSettings(settings: LocalSettings) {
  if (!isBrowser()) {
    return;
  }
  localStorage.setItem(LOCAL_SETTINGS_KEY, JSON.stringify(settings));
}
