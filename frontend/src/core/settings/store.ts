import {
  DEFAULT_LOCAL_SETTINGS,
  LOCAL_SETTINGS_KEY,
  THREAD_CONTEXT_COMPRESSION_KEY_PREFIX,
  THREAD_MODEL_KEY_PREFIX,
  getThreadContextCompressionEnabled,
  getLocalSettings,
  getThreadModelName,
  saveThreadContextCompressionEnabled,
  saveLocalSettings,
  saveThreadModelName,
  type LocalSettings,
} from "./local";

type Listener = () => void;

export type LocalSettingsSetter = <K extends keyof LocalSettings>(
  key: K,
  value: Partial<LocalSettings[K]>,
) => void;

const listeners = new Set<Listener>();
const threadModelNames = new Map<string, string | undefined>();
const threadContextCompressionFlags = new Map<string, boolean | undefined>();

let baseSettings: LocalSettings = DEFAULT_LOCAL_SETTINGS;
let baseSettingsLoaded = false;
let storageListenerRegistered = false;

function emitChange() {
  for (const listener of listeners) {
    listener();
  }
}

function ensureBaseSettingsLoaded() {
  if (baseSettingsLoaded || typeof window === "undefined") {
    return;
  }

  baseSettings = getLocalSettings();
  baseSettingsLoaded = true;
}

function ensureStorageListenerRegistered() {
  if (storageListenerRegistered || typeof window === "undefined") {
    return;
  }

  window.addEventListener("storage", handleStorage);
  storageListenerRegistered = true;
}

function mergeSettingsSection<K extends keyof LocalSettings>(
  settings: LocalSettings,
  key: K,
  value: Partial<LocalSettings[K]>,
): LocalSettings {
  return {
    ...settings,
    [key]: {
      ...settings[key],
      ...value,
    },
  } as LocalSettings;
}

function handleStorage(event: StorageEvent) {
  if (event.storageArea && event.storageArea !== localStorage) {
    return;
  }

  ensureBaseSettingsLoaded();

  if (event.key === null) {
    baseSettings = getLocalSettings();
    threadModelNames.clear();
    threadContextCompressionFlags.clear();
    emitChange();
    return;
  }

  if (event.key === LOCAL_SETTINGS_KEY) {
    baseSettings = getLocalSettings();
    emitChange();
    return;
  }

  if (!event.key.startsWith(THREAD_MODEL_KEY_PREFIX)) {
    if (!event.key.startsWith(THREAD_CONTEXT_COMPRESSION_KEY_PREFIX)) {
      return;
    }
    const threadId = event.key.slice(THREAD_CONTEXT_COMPRESSION_KEY_PREFIX.length);
    threadContextCompressionFlags.set(
      threadId,
      getThreadContextCompressionEnabled(threadId),
    );
    emitChange();
    return;
  }

  const threadId = event.key.slice(THREAD_MODEL_KEY_PREFIX.length);
  threadModelNames.set(threadId, getThreadModelName(threadId));
  emitChange();
}

export function subscribe(listener: Listener): () => void {
  ensureBaseSettingsLoaded();
  ensureStorageListenerRegistered();
  listeners.add(listener);

  return () => {
    listeners.delete(listener);
  };
}

export function getBaseSettingsSnapshot(): LocalSettings {
  ensureBaseSettingsLoaded();
  return baseSettings;
}

export function getThreadModelSnapshot(threadId: string): string | undefined {
  ensureBaseSettingsLoaded();

  if (!threadModelNames.has(threadId)) {
    threadModelNames.set(threadId, getThreadModelName(threadId));
  }

  return threadModelNames.get(threadId);
}

export function getThreadContextCompressionSnapshot(
  threadId: string,
): boolean | undefined {
  ensureBaseSettingsLoaded();

  if (!threadContextCompressionFlags.has(threadId)) {
    threadContextCompressionFlags.set(
      threadId,
      getThreadContextCompressionEnabled(threadId),
    );
  }

  return threadContextCompressionFlags.get(threadId);
}

export const updateLocalSettings: LocalSettingsSetter = (key, value) => {
  ensureBaseSettingsLoaded();
  ensureStorageListenerRegistered();

  baseSettings = mergeSettingsSection(baseSettings, key, value);
  saveLocalSettings(baseSettings);
  emitChange();
};

export function updateThreadSettings<K extends keyof LocalSettings>(
  threadId: string,
  key: K,
  value: Partial<LocalSettings[K]>,
) {
  ensureBaseSettingsLoaded();
  ensureStorageListenerRegistered();

  let nextBaseSettings: LocalSettings;
  if (
    key === "context" &&
    (Object.prototype.hasOwnProperty.call(value, "model_name") ||
      Object.prototype.hasOwnProperty.call(
        value,
        "context_compression_enabled",
      ))
  ) {
    const contextValue = value as Partial<LocalSettings["context"]>;
    nextBaseSettings = mergeSettingsSection(baseSettings, "context", {
      ...contextValue,
      model_name: baseSettings.context.model_name,
      context_compression_enabled:
        baseSettings.context.context_compression_enabled,
    });
  } else {
    nextBaseSettings = mergeSettingsSection(baseSettings, key, value);
  }
  baseSettings = nextBaseSettings;
  saveLocalSettings(baseSettings);

  if (
    key === "context" &&
    Object.prototype.hasOwnProperty.call(value, "model_name")
  ) {
    const contextValue = value as Partial<LocalSettings["context"]>;
    const threadModelName = contextValue.model_name;
    threadModelNames.set(threadId, threadModelName);
    saveThreadModelName(threadId, threadModelName);
  }

  if (
    key === "context" &&
    Object.prototype.hasOwnProperty.call(value, "context_compression_enabled")
  ) {
    const contextValue = value as Partial<LocalSettings["context"]>;
    const enabled =
      contextValue.context_compression_enabled as boolean | undefined;
    threadContextCompressionFlags.set(threadId, enabled);
    saveThreadContextCompressionEnabled(threadId, enabled);
  }

  emitChange();
}
