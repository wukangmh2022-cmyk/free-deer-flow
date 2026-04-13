export function mapSandboxTextToWorkspace(
  text: string,
  workspaceTargetPath?: string | null,
  workspaceContainerPath?: string | null,
) {
  const hostPath = workspaceTargetPath?.trim();
  if (!hostPath) {
    return text;
  }

  let mapped = text;
  const prefixes = [
    workspaceContainerPath?.trim() || null,
    "/mnt/user-data/workspace",
    "/mnt/user-data/uploads",
    "/mnt/user-data/outputs",
  ].filter((value): value is string => Boolean(value));

  for (const prefix of prefixes) {
    mapped = mapped.split(prefix).join(hostPath);
  }

  return mapped
    .replace(/\buploads directory\b/gi, "workspace directory")
    .replace(/\buploads folder\b/gi, "workspace folder")
    .replace(/\boutput directory\b/gi, "workspace directory")
    .replace(/\boutputs directory\b/gi, "workspace directory")
    .replace(/\boutputs folder\b/gi, "workspace folder");
}
