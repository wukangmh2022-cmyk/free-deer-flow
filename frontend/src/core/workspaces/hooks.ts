import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { loadWorkspaces } from "./api";
import type { Workspace } from "./types";

const WORKSPACES_CACHE_KEY = "deerflow:desktop:workspaces-cache";

function readWorkspacesCache(): Workspace[] | undefined {
  if (typeof window === "undefined") {
    return undefined;
  }
  try {
    const raw = window.sessionStorage.getItem(WORKSPACES_CACHE_KEY);
    if (!raw) {
      return undefined;
    }
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Workspace[]) : undefined;
  } catch {
    return undefined;
  }
}

function writeWorkspacesCache(workspaces: Workspace[] | undefined): void {
  if (typeof window === "undefined" || !workspaces) {
    return;
  }
  try {
    window.sessionStorage.setItem(
      WORKSPACES_CACHE_KEY,
      JSON.stringify(workspaces),
    );
  } catch {
    // Ignore storage failures; this is only a UI cache.
  }
}

export function useWorkspaces() {
  const query = useQuery({
    queryKey: ["workspaces"],
    queryFn: loadWorkspaces,
    initialData: readWorkspacesCache,
    placeholderData: (previousData) => previousData,
    staleTime: 10_000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    writeWorkspacesCache(query.data);
  }, [query.data]);

  return {
    ...query,
    workspaces: query.data ?? [],
  };
}
