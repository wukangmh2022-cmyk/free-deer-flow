import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { loadModels } from "./api";
import type { Model } from "./types";

const MODELS_CACHE_KEY = "deerflow:desktop:models-cache";

function readModelsCache(): Model[] | undefined {
  if (typeof window === "undefined") {
    return undefined;
  }
  try {
    const raw = window.sessionStorage.getItem(MODELS_CACHE_KEY);
    if (!raw) {
      return undefined;
    }
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Model[]) : undefined;
  } catch {
    return undefined;
  }
}

function writeModelsCache(models: Model[] | undefined): void {
  if (typeof window === "undefined" || !models) {
    return;
  }
  try {
    window.sessionStorage.setItem(MODELS_CACHE_KEY, JSON.stringify(models));
  } catch {
    // Ignore storage failures; this is only a UI cache.
  }
}

export function useModels({ enabled = true }: { enabled?: boolean } = {}) {
  const query = useQuery({
    queryKey: ["models"],
    queryFn: () => loadModels(),
    enabled,
    initialData: readModelsCache,
    placeholderData: (previousData) => previousData,
    staleTime: 10_000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    writeModelsCache(query.data);
  }, [query.data]);

  return { models: query.data ?? [], isLoading: query.isLoading, error: query.error };
}
