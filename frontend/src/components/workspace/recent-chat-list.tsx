"use client";

import {
  Download,
  FileJson,
  FileText,
  FolderIcon,
  MoreHorizontal,
  Pencil,
  Plus,
  Share2,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { getAPIClient } from "@/core/api";
import { useI18n } from "@/core/i18n/hooks";
import {
  exportThreadAsJSON,
  exportThreadAsMarkdown,
} from "@/core/threads/export";
import {
  useDeleteThread,
  useRenameThread,
  useThreads,
} from "@/core/threads/hooks";
import type { AgentThread, AgentThreadState } from "@/core/threads/types";
import {
  pathOfThread,
  titleOfThread,
  workspaceMetadataOfThread,
  workspaceLabelOfThread,
} from "@/core/threads/utils";
import { env } from "@/env";
import { isIMEComposing } from "@/lib/ime";

export function RecentChatList() {
  const { t } = useI18n();
  const router = useRouter();
  const pathname = usePathname();
  const { thread_id: threadIdFromPath } = useParams<{ thread_id: string }>();
  const { data: threads = [] } = useThreads();
  const { mutate: deleteThread } = useDeleteThread();
  const { mutate: renameThread } = useRenameThread();

  const [renameThreadId, setRenameThreadId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const handleDelete = useCallback(
    (threadId: string) => {
      deleteThread({ threadId });
      if (threadId === threadIdFromPath) {
        const threadIndex = threads.findIndex((t) => t.thread_id === threadId);
        let nextThreadId = "new";
        if (threadIndex > -1) {
          if (threads[threadIndex + 1]) {
            nextThreadId = threads[threadIndex + 1]!.thread_id;
          } else if (threads[threadIndex - 1]) {
            nextThreadId = threads[threadIndex - 1]!.thread_id;
          }
        }
        void router.push(`/workspace/chats/${nextThreadId}`);
      }
    },
    [deleteThread, router, threadIdFromPath, threads],
  );

  const handleRenameClick = useCallback(
    (threadId: string, currentTitle: string) => {
      setRenameThreadId(threadId);
      setRenameValue(currentTitle);
    },
    [],
  );

  const handleRenameSubmit = useCallback(() => {
    if (renameThreadId && renameValue.trim()) {
      renameThread({ threadId: renameThreadId, title: renameValue.trim() });
    }
    setRenameThreadId(null);
    setRenameValue("");
  }, [renameThread, renameThreadId, renameValue]);

  const handleRenameCancel = useCallback(() => {
    setRenameThreadId(null);
    setRenameValue("");
  }, []);

  const isInlineEditing = useCallback(
    (threadId: string) => renameThreadId === threadId,
    [renameThreadId],
  );

  const handleShare = useCallback(
    async (threadId: string) => {
      // Always use Vercel URL for sharing so others can access
      const VERCEL_URL = "https://deer-flow-v2.vercel.app";
      const isLocalhost =
        window.location.hostname === "localhost" ||
        window.location.hostname === "127.0.0.1";
      // On localhost: use Vercel URL; On production: use current origin
      const baseUrl = isLocalhost ? VERCEL_URL : window.location.origin;
      const shareUrl = `${baseUrl}/workspace/chats/${threadId}`;
      try {
        await navigator.clipboard.writeText(shareUrl);
        toast.success(t.clipboard.linkCopied);
      } catch {
        toast.error(t.clipboard.failedToCopyToClipboard);
      }
    },
    [t],
  );

  const handleExport = useCallback(
    async (thread: AgentThread, format: "markdown" | "json") => {
      try {
        const apiClient = getAPIClient();
        const state = await apiClient.threads.getState<AgentThreadState>(
          thread.thread_id,
        );
        const messages = state.values?.messages ?? [];
        if (messages.length === 0) {
          toast.error(t.conversation.noMessages);
          return;
        }
        if (format === "markdown") {
          exportThreadAsMarkdown(thread, messages);
        } else {
          exportThreadAsJSON(thread, messages);
        }
        toast.success(t.common.exportSuccess);
      } catch {
        toast.error("Failed to export conversation");
      }
    },
    [t],
  );

  if (threads.length === 0) {
    return null;
  }

  const groupedThreads = threads.reduce<
    Record<
      string,
      {
        label: string;
        workspacePath?: string;
        items: AgentThread[];
      }
    >
  >((groups, thread) => {
    const metadata = workspaceMetadataOfThread(thread);
    const label = workspaceLabelOfThread(thread);
    const groupKey = metadata.workspace_path
      ? `${label}::${metadata.workspace_path}`
      : label;
    const group =
      groups[groupKey] ??
      (groups[groupKey] = {
        label,
        workspacePath: metadata.workspace_path,
        items: [],
      });
    group.items.push(thread);
    return groups;
  }, {});
  const groupedEntries = Object.entries(groupedThreads).sort(([, a], [, b]) =>
    a.label.localeCompare(b.label, "zh-CN"),
  );

  return (
    <>
      <SidebarGroup>
        <SidebarGroupLabel>
          {env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true"
            ? t.sidebar.recentChats
            : t.sidebar.demoChats}
        </SidebarGroupLabel>
        <SidebarGroupContent className="group-data-[collapsible=icon]:pointer-events-none group-data-[collapsible=icon]:-mt-8 group-data-[collapsible=icon]:opacity-0">
          <SidebarMenu>
            <div className="flex w-full flex-col gap-3">
              {groupedEntries.map(([groupKey, group]) => (
                <div key={groupKey} className="flex flex-col gap-1.5">
                  <div className="text-foreground/85 flex items-center justify-between gap-2 px-2 text-xs font-semibold tracking-wide">
                    <div className="flex min-w-0 items-center gap-2">
                      <FolderIcon className="size-3.5" />
                      <span className="truncate">{group.label}</span>
                    </div>
                    {group.workspacePath ? (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground hover:text-foreground size-6 rounded-md"
                        asChild
                      >
                        <Link
                          href={`/workspace/chats/new?workspace=${encodeURIComponent(group.workspacePath)}`}
                          title={`在 ${group.label} 下新建对话`}
                        >
                          <Plus className="size-3.5" />
                        </Link>
                      </Button>
                    ) : null}
                  </div>
                  <div className="border-border/50 ml-3 flex flex-col gap-1 border-l pl-3">
                    {group.items.map((thread) => {
                      const isActive = pathOfThread(thread.thread_id) === pathname;
                      const isEditing = isInlineEditing(thread.thread_id);
                      const currentTitle = titleOfThread(thread);
                      return (
                        <SidebarMenuItem
                          key={thread.thread_id}
                          className="group/side-menu-item"
                        >
                          <div className="relative flex items-center gap-1">
                            {isEditing ? (
                              <div className="min-w-0 flex-1">
                                <Input
                                  autoFocus
                                  value={renameValue}
                                  onClick={(event) => {
                                    event.preventDefault();
                                    event.stopPropagation();
                                  }}
                                  onChange={(event) =>
                                    setRenameValue(event.target.value)
                                  }
                                  onBlur={handleRenameSubmit}
                                  onKeyDown={(event) => {
                                    if (
                                      event.key === "Enter" &&
                                      !isIMEComposing(event)
                                    ) {
                                      event.preventDefault();
                                      handleRenameSubmit();
                                      return;
                                    }
                                    if (event.key === "Escape") {
                                      event.preventDefault();
                                      handleRenameCancel();
                                    }
                                  }}
                                  className="bg-background h-8 w-full rounded-lg text-sm"
                                />
                              </div>
                            ) : (
                              <SidebarMenuButton
                                isActive={isActive}
                                asChild
                                className="min-h-8 flex-1 rounded-lg"
                              >
                                <Link
                                  className="text-muted-foreground block w-full whitespace-nowrap group-hover/side-menu-item:overflow-hidden"
                                  href={pathOfThread(thread.thread_id)}
                                  onDoubleClick={(event) => {
                                    event.preventDefault();
                                    event.stopPropagation();
                                    handleRenameClick(thread.thread_id, currentTitle);
                                  }}
                                  title="双击修改标题"
                                >
                                  <span className="block truncate">
                                    {currentTitle}
                                  </span>
                                </Link>
                              </SidebarMenuButton>
                            )}
                            {env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true" && (
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <SidebarMenuAction
                                    showOnHover
                                    className="bg-background/50 hover:bg-background"
                                  >
                                    <MoreHorizontal />
                                    <span className="sr-only">{t.common.more}</span>
                                  </SidebarMenuAction>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent
                                  className="w-48 rounded-lg"
                                  side={"right"}
                                  align={"start"}
                                >
                                  <DropdownMenuItem
                                    onSelect={() =>
                                      handleRenameClick(
                                        thread.thread_id,
                                        currentTitle,
                                      )
                                    }
                                  >
                                    <Pencil className="text-muted-foreground" />
                                    <span>{t.common.rename}</span>
                                  </DropdownMenuItem>
                                  <DropdownMenuItem
                                    onSelect={() => handleShare(thread.thread_id)}
                                  >
                                    <Share2 className="text-muted-foreground" />
                                    <span>{t.common.share}</span>
                                  </DropdownMenuItem>
                                  <DropdownMenuSub>
                                    <DropdownMenuSubTrigger>
                                      <Download className="text-muted-foreground" />
                                      <span>{t.common.export}</span>
                                    </DropdownMenuSubTrigger>
                                    <DropdownMenuSubContent>
                                      <DropdownMenuItem
                                        onSelect={() =>
                                          handleExport(thread, "markdown")
                                        }
                                      >
                                        <FileText className="text-muted-foreground" />
                                        <span>{t.common.exportAsMarkdown}</span>
                                      </DropdownMenuItem>
                                      <DropdownMenuItem
                                        onSelect={() => handleExport(thread, "json")}
                                      >
                                        <FileJson className="text-muted-foreground" />
                                        <span>{t.common.exportAsJSON}</span>
                                      </DropdownMenuItem>
                                    </DropdownMenuSubContent>
                                  </DropdownMenuSub>
                                  <DropdownMenuSeparator />
                                  <DropdownMenuItem
                                    onSelect={() => handleDelete(thread.thread_id)}
                                  >
                                    <Trash2 className="text-muted-foreground" />
                                    <span>{t.common.delete}</span>
                                  </DropdownMenuItem>
                                </DropdownMenuContent>
                              </DropdownMenu>
                            )}
                          </div>
                        </SidebarMenuItem>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    </>
  );
}
