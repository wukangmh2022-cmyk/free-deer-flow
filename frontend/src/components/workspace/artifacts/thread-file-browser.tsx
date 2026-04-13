"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeftIcon, FileIcon, FolderIcon, RefreshCcwIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { browseWorkspace } from "@/core/workspaces/api";
import type { Workspace } from "@/core/workspaces/types";

function WorkspaceRow({
  item,
  onOpen,
}: {
  item: Workspace;
  onOpen?: (item: Workspace) => void;
}) {
  const isDirectory = !item.label.includes(".");
  return (
    <button
      type="button"
      className="hover:bg-muted flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-sm"
      onClick={() => onOpen?.(item)}
      disabled={!onOpen}
    >
      <div className="flex min-w-0 items-center gap-2">
        {isDirectory ? (
          <FolderIcon className="size-4 shrink-0" />
        ) : (
          <FileIcon className="size-4 shrink-0" />
        )}
        <div className="min-w-0">
          <div className="truncate font-medium">{item.label}</div>
          <div className="text-muted-foreground truncate text-xs">
            {item.host_path}
          </div>
        </div>
      </div>
    </button>
  );
}

export function ThreadFileBrowser({
  workspaceTargetPath,
}: {
  workspaceTargetPath?: string | null;
}) {
  const workspacePath = workspaceTargetPath?.trim() || null;
  const [browsingPath, setBrowsingPath] = useState<string | null>(workspacePath);

  useEffect(() => {
    setBrowsingPath(workspacePath);
  }, [workspacePath]);

  const browseQuery = useQuery({
    queryKey: ["workspace-browser", browsingPath],
    enabled: Boolean(browsingPath),
    queryFn: async () => browseWorkspace(browsingPath!),
  });

  if (!workspacePath) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        当前对话还没有绑定工作目录。
      </div>
    );
  }

  const data = browseQuery.data;
  const entries = data?.entries ?? [];
  const canGoUp = Boolean(
    data?.parent &&
      browsingPath &&
      workspacePath &&
      browsingPath !== workspacePath,
  );

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-lg font-medium">Workspace</div>
        </div>
        <Button
          size="icon-sm"
          variant="ghost"
          onClick={() => void browseQuery.refetch()}
          disabled={browseQuery.isRefetching}
        >
          <RefreshCcwIcon className="size-4" />
        </Button>
      </div>

      <div className="rounded-lg border px-3 py-2">
        <div className="text-sm font-medium">{browsingPath}</div>
      </div>

      <ScrollArea className="min-h-0 grow rounded-lg border">
        <div className="space-y-2 p-3">
          {canGoUp && data?.parent && (
            <button
              type="button"
              className="hover:bg-muted flex w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm"
              onClick={() => setBrowsingPath(data.parent!.host_path)}
            >
              <ChevronLeftIcon className="size-4" />
              返回上一级
            </button>
          )}
          {browseQuery.isLoading ? (
            <div className="text-muted-foreground py-8 text-center text-sm">
              正在读取工作目录...
            </div>
          ) : entries.length === 0 ? (
            <div className="text-muted-foreground py-8 text-center text-sm">
              当前工作目录下还没有文件。
            </div>
          ) : (
            entries.map((item) => (
              <WorkspaceRow
                key={item.id}
                item={item}
                onOpen={
                  data?.children.some((child) => child.host_path === item.host_path)
                    ? (next) => setBrowsingPath(next.host_path)
                    : undefined
                }
              />
            ))
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
