import { getBackendBaseURL } from "../config";

import type { Workspace, WorkspaceBrowseResponse } from "./types";

export async function loadWorkspaces() {
  const res = await fetch(`${getBackendBaseURL()}/api/workspaces`);
  if (!res.ok) {
    throw new Error("Failed to load workspaces.");
  }
  const { workspaces } = (await res.json()) as { workspaces: Workspace[] };
  return workspaces;
}

export async function browseWorkspace(path: string) {
  const url = new URL(`${getBackendBaseURL()}/api/workspaces/browse`, window.location.origin);
  url.searchParams.set("path", path);
  const res = await fetch(url.toString());
  if (!res.ok) {
    throw new Error("Failed to browse workspace.");
  }
  return (await res.json()) as WorkspaceBrowseResponse;
}
