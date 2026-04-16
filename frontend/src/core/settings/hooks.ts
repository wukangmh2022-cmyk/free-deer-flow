import { useCallback, useMemo, useSyncExternalStore } from "react";

import {
  DEFAULT_LOCAL_SETTINGS,
  applyThreadContextOverrides,
  type LocalSettings,
} from "./local";
import {
  getBaseSettingsSnapshot,
  getThreadContextCompressionSnapshot,
  getThreadModelSnapshot,
  subscribe,
  updateLocalSettings,
  updateThreadSettings,
  type LocalSettingsSetter,
} from "./store";

export function useLocalSettings(): [LocalSettings, LocalSettingsSetter] {
  const settings = useSyncExternalStore(
    subscribe,
    getBaseSettingsSnapshot,
    () => DEFAULT_LOCAL_SETTINGS,
  );

  const setSettings = useCallback<LocalSettingsSetter>((key, value) => {
    updateLocalSettings(key, value);
  }, []);

  return [settings, setSettings];
}

export function useThreadSettings(
  threadId: string,
): [LocalSettings, LocalSettingsSetter] {
  const baseSettings = useSyncExternalStore(
    subscribe,
    getBaseSettingsSnapshot,
    () => DEFAULT_LOCAL_SETTINGS,
  );

  const threadModelName = useSyncExternalStore(
    subscribe,
    () => getThreadModelSnapshot(threadId),
    () => undefined,
  );

  const threadContextCompressionEnabled = useSyncExternalStore(
    subscribe,
    () => getThreadContextCompressionSnapshot(threadId),
    () => undefined,
  );

  const settings = useMemo(
    () =>
      applyThreadContextOverrides(baseSettings, {
        model_name: threadModelName,
        context_compression_enabled: threadContextCompressionEnabled,
      }),
    [baseSettings, threadContextCompressionEnabled, threadModelName],
  );

  const setSettings = useCallback<LocalSettingsSetter>(
    (key, value) => {
      updateThreadSettings(threadId, key, value);
    },
    [threadId],
  );

  return [settings, setSettings];
}
