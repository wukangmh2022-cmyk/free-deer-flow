import { getBackendBaseURL } from "../config";

import type {
  CreateWorkspaceFolderRequest,
  Workspace,
  WorkspaceBrowseResponse,
} from "./types";

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

export async function createWorkspaceFolder(
  request: CreateWorkspaceFolderRequest,
) {
  const res = await fetch(`${getBackendBaseURL()}/api/workspaces/folders`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
  });

  if (!res.ok) {
    const error = await res
      .json()
      .catch(() => ({ detail: "Failed to create folder." }));
    throw new Error(error.detail ?? "Failed to create folder.");
  }

  return (await res.json()) as Workspace;
}
