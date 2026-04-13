import { getBackendBaseURL } from "../config";

import type {
  MirrorThreadFilesResponse,
  ThreadFileScope,
  ThreadFilesResponse,
} from "./types";

export async function loadThreadFiles(threadId: string): Promise<ThreadFilesResponse> {
  const url = new URL(
    `${getBackendBaseURL()}/api/threads/${encodeURIComponent(threadId)}/files`,
    window.location.origin,
  );
  const res = await fetch(url.toString());
  if (!res.ok) {
    throw new Error("Failed to load thread files.");
  }
  return (await res.json()) as ThreadFilesResponse;
}

export async function mirrorThreadFiles(
  threadId: string,
  scope: ThreadFileScope,
  destinationPath?: string,
): Promise<MirrorThreadFilesResponse> {
  const res = await fetch(
    `${getBackendBaseURL()}/api/threads/${encodeURIComponent(threadId)}/files/mirror`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope,
        ...(destinationPath ? { destination_path: destinationPath } : {}),
      }),
    },
  );
  if (!res.ok) {
    const error = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(error.detail ?? "Failed to mirror thread files.");
  }
  return (await res.json()) as MirrorThreadFilesResponse;
}
