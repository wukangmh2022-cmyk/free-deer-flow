"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  CheckIcon,
  ChevronRightIcon,
  FolderIcon,
  FolderOpenIcon,
  FolderPlusIcon,
  Loader2Icon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  browseWorkspace,
  createWorkspaceFolder,
} from "@/core/workspaces/api";
import type { Workspace } from "@/core/workspaces/types";
import { cn } from "@/lib/utils";

type WorkspacePickerDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  value: Workspace | undefined;
  fallbackWorkspace: Workspace | undefined;
  onConfirm: (workspace: Workspace) => void;
};

export function WorkspacePickerDialog({
  open,
  onOpenChange,
  value,
  fallbackWorkspace,
  onConfirm,
}: WorkspacePickerDialogProps) {
  const initialWorkspace = value ?? fallbackWorkspace;
  const [browsingPath, setBrowsingPath] = useState<string | null>(null);
  const [createFolderOpen, setCreateFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");

  useEffect(() => {
    if (!open) {
      return;
    }
    setBrowsingPath((value ?? fallbackWorkspace)?.host_path ?? null);
  }, [fallbackWorkspace, open, value]);

  useEffect(() => {
    if (!open) {
      setCreateFolderOpen(false);
      setNewFolderName("");
    }
  }, [open]);

  const browseQuery = useQuery({
    queryKey: ["workspaces", "browse", browsingPath],
    enabled: open && Boolean(browsingPath),
    queryFn: async () => browseWorkspace(browsingPath!),
  });

  const createFolderMutation = useMutation({
    mutationFn: async () => {
      if (!browsingPath) {
        throw new Error("当前目录不可用。");
      }
      return createWorkspaceFolder({
        path: browsingPath,
        name: newFolderName,
      });
    },
    onSuccess: async (workspace) => {
      toast.success(`已创建文件夹 ${workspace.label}`);
      setCreateFolderOpen(false);
      setNewFolderName("");
      await browseQuery.refetch();
      setBrowsingPath(workspace.host_path);
    },
    onError: (error) => {
      toast.error(
        error instanceof Error ? error.message : "创建文件夹失败。",
      );
    },
  });

  const data = browseQuery.data;
  const isLoading = browseQuery.isLoading;
  const currentWorkspace = data?.current ?? initialWorkspace;
  const parentWorkspace = data?.parent ?? null;
  const children = data?.children ?? [];
  const pathSegments = useMemo(() => {
    if (!currentWorkspace?.host_path) {
      return [];
    }
    return currentWorkspace.host_path.split("/").filter(Boolean);
  }, [currentWorkspace?.host_path]);
  const displayPath =
    pathSegments.length > 0
      ? pathSegments.join(" / ")
      : (currentWorkspace?.label ?? "未选择");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[calc(100vw-2rem)] overflow-hidden sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>选择工作目录</DialogTitle>
          <DialogDescription>
            点击文件夹进入目录，点击“确定”后才会把当前目录设为本对话的工作目录。
          </DialogDescription>
        </DialogHeader>

        <div className="min-w-0 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <div className="text-muted-foreground text-xs">
              在当前目录中浏览或新建子文件夹
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setCreateFolderOpen((value) => !value)}
              disabled={!currentWorkspace || createFolderMutation.isPending}
            >
              <FolderPlusIcon className="size-4" />
              新建文件夹
            </Button>
          </div>

          {createFolderOpen && (
            <div className="bg-muted/40 rounded-lg border p-3">
              <div className="flex flex-col gap-3 sm:flex-row">
                <Input
                  value={newFolderName}
                  onChange={(event) => setNewFolderName(event.target.value)}
                  placeholder="输入新文件夹名称"
                  disabled={createFolderMutation.isPending}
                  onKeyDown={(event) => {
                    if (
                      event.key === "Enter" &&
                      newFolderName.trim() &&
                      !createFolderMutation.isPending
                    ) {
                      event.preventDefault();
                      createFolderMutation.mutate();
                    }
                  }}
                />
                <Button
                  type="button"
                  onClick={() => createFolderMutation.mutate()}
                  disabled={
                    !newFolderName.trim() || createFolderMutation.isPending
                  }
                >
                  {createFolderMutation.isPending ? (
                    <Loader2Icon className="size-4 animate-spin" />
                  ) : (
                    <FolderPlusIcon className="size-4" />
                  )}
                  创建
                </Button>
              </div>
            </div>
          )}

          <div className="bg-muted/40 min-w-0 rounded-lg border px-3 py-2 text-sm">
            <div className="text-muted-foreground mb-1 text-xs">当前目录</div>
            <div className="flex min-w-0 items-start gap-2 overflow-hidden">
              <FolderOpenIcon className="text-muted-foreground size-4 shrink-0" />
              <span className="min-w-0 break-all font-medium whitespace-normal">
                {displayPath}
              </span>
            </div>
          </div>

          <ScrollArea className="h-80 min-w-0 rounded-lg border">
            <div className="min-w-0 p-2">
              {parentWorkspace && (
                <button
                  type="button"
                  className="hover:bg-muted flex min-w-0 w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm"
                  onClick={() => setBrowsingPath(parentWorkspace.host_path)}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <FolderIcon className="size-4 shrink-0" />
                    <span>..</span>
                  </div>
                  <span className="text-muted-foreground shrink-0 text-xs">上一级</span>
                </button>
              )}

              {children.map((workspace) => (
                <button
                  key={workspace.id}
                  type="button"
                  className={cn(
                    "hover:bg-muted flex min-w-0 w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm",
                    currentWorkspace?.id === workspace.id && "bg-muted",
                  )}
                  onClick={() => setBrowsingPath(workspace.host_path)}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <FolderIcon className="size-4 shrink-0" />
                    <span className="truncate">{workspace.label}</span>
                  </div>
                  <ChevronRightIcon className="text-muted-foreground size-4 shrink-0" />
                </button>
              ))}

              {!isLoading && children.length === 0 && (
                <div className="text-muted-foreground px-3 py-8 text-center text-sm">
                  当前目录下没有可进入的子文件夹。
                </div>
              )}

              {isLoading && (
                <div className="text-muted-foreground px-3 py-8 text-center text-sm">
                  正在读取目录...
                </div>
              )}
            </div>
          </ScrollArea>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            disabled={!currentWorkspace}
            onClick={() => {
              if (!currentWorkspace) {
                return;
              }
              onConfirm(currentWorkspace);
              onOpenChange(false);
            }}
          >
            <CheckIcon className="mr-2 size-4" />
            确定当前目录
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
