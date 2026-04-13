export interface Workspace {
  id: string;
  label: string;
  host_path: string;
  container_path: string;
  read_only: boolean;
  source: string;
}

export interface WorkspaceBrowseResponse {
  current: Workspace;
  parent: Workspace | null;
  children: Workspace[];
  entries: Workspace[];
}
