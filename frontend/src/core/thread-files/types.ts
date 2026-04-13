export type ThreadFileScope = "workspace" | "uploads" | "outputs";

export interface ThreadFileEntry {
  name: string;
  relative_path: string;
  host_path: string;
  is_dir: boolean;
  size: number | null;
  modified_at: number | null;
}

export interface ThreadFileScopeData {
  scope: ThreadFileScope;
  root_path: string;
  mapped_to_workspace: boolean;
  entries: ThreadFileEntry[];
}

export interface ThreadFilesResponse {
  workspace_target_path: string | null;
  scopes: ThreadFileScopeData[];
}

export interface MirrorThreadFilesResponse {
  success: boolean;
  scope: ThreadFileScope;
  source_path: string;
  destination_path: string;
  mirrored_files: number;
  message: string;
}
