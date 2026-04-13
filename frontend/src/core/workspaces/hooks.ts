import { useQuery } from "@tanstack/react-query";

import { loadWorkspaces } from "./api";

export function useWorkspaces() {
  const query = useQuery({
    queryKey: ["workspaces"],
    queryFn: loadWorkspaces,
  });

  return {
    ...query,
    workspaces: query.data ?? [],
  };
}
