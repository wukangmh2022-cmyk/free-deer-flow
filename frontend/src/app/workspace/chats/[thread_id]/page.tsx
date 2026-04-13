"use client";

import { FolderIcon } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { type PromptInputMessage } from "@/components/ai-elements/prompt-input";
import { Button } from "@/components/ui/button";
import {
  ArtifactTrigger,
  ThreadFilesTrigger,
} from "@/components/workspace/artifacts";
import {
  ChatBox,
  useSpecificChatMode,
  useThreadChat,
} from "@/components/workspace/chats";
import { ExportTrigger } from "@/components/workspace/export-trigger";
import { InputBox } from "@/components/workspace/input-box";
import {
  MessageList,
  MESSAGE_LIST_DEFAULT_PADDING_BOTTOM,
  MESSAGE_LIST_FOLLOWUPS_EXTRA_PADDING_BOTTOM,
} from "@/components/workspace/messages";
import { ThreadContext } from "@/components/workspace/messages/context";
import { ThreadTitle } from "@/components/workspace/thread-title";
import { TodoList } from "@/components/workspace/todo-list";
import { TokenUsageIndicator } from "@/components/workspace/token-usage-indicator";
import { Welcome } from "@/components/workspace/welcome";
import { WorkspacePickerDialog } from "@/components/workspace/workspace-picker-dialog";
import { useI18n } from "@/core/i18n/hooks";
import { useNotification } from "@/core/notification/hooks";
import { useThreadSettings } from "@/core/settings";
import { mirrorThreadFiles } from "@/core/thread-files/api";
import { useThreadRecord, useThreadStream } from "@/core/threads/hooks";
import { textOfMessage, workspaceMetadataOfThread } from "@/core/threads/utils";
import { uuid } from "@/core/utils/uuid";
import { useWorkspaces } from "@/core/workspaces";
import type { Workspace } from "@/core/workspaces/types";
import { env } from "@/env";
import { cn } from "@/lib/utils";

export default function ChatPage() {
  const { t } = useI18n();
  const searchParams = useSearchParams();
  const [showFollowups, setShowFollowups] = useState(false);
  const { threadId, setThreadId, isNewThread, setIsNewThread, isMock } =
    useThreadChat();
  const [settings, setSettings] = useThreadSettings(threadId);
  const [mounted, setMounted] = useState(false);
  const { workspaces } = useWorkspaces();
  const threadRecordQuery = useThreadRecord(isNewThread ? undefined : threadId);
  const threadRecord = threadRecordQuery.data;
  const resolvedThreadId =
    isNewThread || !threadRecordQuery.isFetched || threadRecord === null
      ? undefined
      : threadId;
  const [selectedWorkspace, setSelectedWorkspace] = useState<Workspace | undefined>(undefined);
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false);
  const requestedWorkspacePath = searchParams.get("workspace");
  const retryContinueRef = useRef<(() => void) | null>(null);
  const retriedHumanMessageIdsRef = useRef<Set<string>>(new Set());
  const outputSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useSpecificChatMode();

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (isNewThread || !threadRecordQuery.isFetched || threadRecord !== null) {
      return;
    }

    setIsNewThread(true);
    setThreadId(uuid());
    history.replaceState(null, "", "/workspace/chats/new");
  }, [
    isNewThread,
    setIsNewThread,
    setThreadId,
    threadRecord,
    threadRecordQuery.isFetched,
  ]);

  const { showNotification } = useNotification();
  const buildWorkspaceContext = useCallback(() => {
    if (!selectedWorkspace) {
      return undefined;
    }
    return {
      workspace_id: selectedWorkspace.id,
      workspace_name: selectedWorkspace.label,
      workspace_path: selectedWorkspace.host_path,
      workspace_container_path: selectedWorkspace.container_path,
    };
  }, [selectedWorkspace]);

  useEffect(() => {
    if (isNewThread && requestedWorkspacePath && workspaces.length > 0) {
      const matchedWorkspace = workspaces.find(
        (workspace) => workspace.host_path === requestedWorkspacePath,
      );
      if (matchedWorkspace && selectedWorkspace?.id !== matchedWorkspace.id) {
        setSelectedWorkspace(matchedWorkspace);
        return;
      }
    }

    if (!isNewThread) {
      const metadata = workspaceMetadataOfThread(threadRecord);
      if (
        metadata.workspace_path &&
        metadata.workspace_name &&
        metadata.workspace_container_path
      ) {
        const nextWorkspace: Workspace = {
          id: metadata.workspace_id ?? metadata.workspace_path,
          label: metadata.workspace_name,
          host_path: metadata.workspace_path,
          container_path: metadata.workspace_container_path,
          read_only: false,
          source: "thread.metadata",
        };
        const hasChanged =
          selectedWorkspace?.id !== nextWorkspace.id ||
          selectedWorkspace?.label !== nextWorkspace.label ||
          selectedWorkspace?.host_path !== nextWorkspace.host_path ||
          selectedWorkspace?.container_path !== nextWorkspace.container_path;

        if (hasChanged) {
          setSelectedWorkspace(nextWorkspace);
        }
      }
      return;
    }
    if (!selectedWorkspace && workspaces[0]) {
      setSelectedWorkspace(workspaces[0]);
    }
  }, [
    isNewThread,
    requestedWorkspacePath,
    selectedWorkspace,
    threadRecord,
    workspaces,
  ]);

  const [thread, sendMessage, isUploading] = useThreadStream({
    threadId: resolvedThreadId,
    context: settings.context,
    isMock,
    initialThreadMetadata: buildWorkspaceContext(),
    onStart: (createdThreadId) => {
      setThreadId(createdThreadId);
      setIsNewThread(false);
      // ! Important: Never use next.js router for navigation in this case, otherwise it will cause the thread to re-mount and lose all states. Use native history API instead.
      history.replaceState(null, "", `/workspace/chats/${createdThreadId}`);
    },
    onFinish: (state) => {
      const lastMessage = state.messages.at(-1);
      if (document.hidden || !document.hasFocus()) {
        let body = "Conversation finished";
        if (lastMessage) {
          const textContent = textOfMessage(lastMessage);
          if (textContent) {
            body =
              textContent.length > 200
                ? textContent.substring(0, 200) + "..."
                : textContent;
          }
        }
        showNotification(state.title, { body });
      }

      const lastVisibleHuman = [...state.messages]
        .reverse()
        .find(
          (message) =>
            message.type === "human" &&
            message.additional_kwargs?.hide_from_ui !== true,
        );
      const lastContent =
        typeof lastMessage?.content === "string"
          ? lastMessage.content.trim()
          : "";
      const lastToolCalls =
        lastMessage?.type === "ai" ? (lastMessage.tool_calls ?? []) : [];
      if (
        lastMessage?.type === "ai" &&
        lastContent.length === 0 &&
        lastToolCalls.length === 0 &&
        lastVisibleHuman?.id &&
        !retriedHumanMessageIdsRef.current.has(lastVisibleHuman.id)
      ) {
        retriedHumanMessageIdsRef.current.add(lastVisibleHuman.id);
        toast("模型返回空结果，正在自动继续一次");
        retryContinueRef.current?.();
      }

      const currentWorkspacePath =
        buildWorkspaceContext()?.workspace_path ??
        workspaceMetadataOfThread(threadRecord).workspace_path;
      if (currentWorkspacePath && currentWorkspacePath.trim()) {
        void mirrorThreadFiles(threadId, "outputs", currentWorkspacePath).catch(
          () => {},
        );
      }
    },
    onToolEnd: ({ name }) => {
      const currentWorkspacePath =
        buildWorkspaceContext()?.workspace_path ??
        workspaceMetadataOfThread(threadRecord).workspace_path;
      if (!currentWorkspacePath || !currentWorkspacePath.trim()) {
        return;
      }
      if (!["write_file", "str_replace", "bash", "present_files"].includes(name)) {
        return;
      }
      if (outputSyncTimerRef.current) {
        clearTimeout(outputSyncTimerRef.current);
      }
      outputSyncTimerRef.current = setTimeout(() => {
        void mirrorThreadFiles(threadId, "outputs", currentWorkspacePath).catch(
          () => {},
        );
      }, 600);
    },
  });

  useEffect(() => {
    retryContinueRef.current = () => {
      void sendMessage(
        threadId,
        { text: "continue", files: [] },
        buildWorkspaceContext(),
        {
          additionalKwargs: {
            hide_from_ui: true,
            internal_retry_reason: "empty_assistant_message",
          },
        },
      );
    };
    return () => {
      retryContinueRef.current = null;
    };
  }, [buildWorkspaceContext, sendMessage, threadId]);

  useEffect(() => {
    return () => {
      if (outputSyncTimerRef.current) {
        clearTimeout(outputSyncTimerRef.current);
      }
    };
  }, []);

  const handleSubmit = useCallback(
    (message: PromptInputMessage) => {
      const workspaceContext = buildWorkspaceContext();
      void (async () => {
        await sendMessage(threadId, message, workspaceContext);
        if (workspaceContext?.workspace_path?.trim()) {
          await mirrorThreadFiles(
            threadId,
            "uploads",
            workspaceContext.workspace_path,
          ).catch(() => {});
        }
      })();
    },
    [buildWorkspaceContext, sendMessage, threadId],
  );
  const handleStop = useCallback(async () => {
    await thread.stop();
  }, [thread]);

  const messageListPaddingBottom = showFollowups
    ? MESSAGE_LIST_DEFAULT_PADDING_BOTTOM +
      MESSAGE_LIST_FOLLOWUPS_EXTRA_PADDING_BOTTOM
    : undefined;
  const workspaceTargetPath =
    selectedWorkspace?.host_path ??
    workspaceMetadataOfThread(threadRecord).workspace_path ??
    null;
  const workspaceContainerPath =
    selectedWorkspace?.container_path ??
    workspaceMetadataOfThread(threadRecord).workspace_container_path ??
    null;

  return (
    <ThreadContext.Provider
      value={{
        thread,
        isMock,
        workspaceTargetPath,
        workspaceContainerPath,
      }}
    >
      <ChatBox threadId={threadId} workspaceTargetPath={workspaceTargetPath}>
        <div className="relative flex size-full min-h-0 justify-between">
          <header
            className={cn(
              "absolute top-0 right-0 left-0 z-30 flex h-12 shrink-0 items-center px-4",
              isNewThread
                ? "bg-background/0 backdrop-blur-none"
                : "bg-background/80 shadow-xs backdrop-blur",
            )}
          >
            <div className="flex w-full min-w-0 flex-col text-sm font-medium">
              <ThreadTitle threadId={threadId} thread={thread} />
              {!isNewThread && (
                <div className="text-muted-foreground truncate text-[11px] font-normal">
                  {workspaceMetadataOfThread(threadRecord).workspace_name ??
                    "未设置工作目录"}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <TokenUsageIndicator messages={thread.messages} />
              <ExportTrigger threadId={threadId} />
              <ThreadFilesTrigger />
              <ArtifactTrigger />
            </div>
          </header>
          <main className="flex min-h-0 max-w-full grow flex-col">
            <div className="flex size-full justify-center">
              <MessageList
                className={cn("size-full", !isNewThread && "pt-10")}
                threadId={threadId}
                thread={thread}
                paddingBottom={messageListPaddingBottom}
              />
            </div>
            <div className="absolute right-0 bottom-0 left-0 z-30 flex justify-center px-4">
              <div
                className={cn(
                  "relative w-full",
                  isNewThread && "-translate-y-[calc(50vh-96px)]",
                  isNewThread
                    ? "max-w-(--container-width-sm)"
                    : "max-w-(--container-width-md)",
                )}
              >
                <div className="absolute -top-4 right-0 left-0 z-0">
                  <div className="absolute right-0 bottom-0 left-0">
                    <TodoList
                      className="bg-background/5"
                      todos={thread.values.todos ?? []}
                      hidden={
                        !thread.values.todos || thread.values.todos.length === 0
                      }
                    />
                  </div>
                </div>
                {mounted ? (
                  <div className="space-y-2">
                    {isNewThread && (
                      <div className="pb-2">
                        <Welcome mode={settings.context.mode} />
                      </div>
                    )}
                    <div className="bg-background/80 flex items-center justify-between rounded-xl border px-3 py-2 shadow-sm backdrop-blur">
                      <div className="text-muted-foreground flex items-center gap-2 text-xs">
                        <FolderIcon className="size-3.5" />
                        <span>工作目录</span>
                      </div>
                      {isNewThread ? (
                        <Button
                          variant="outline"
                          className="h-8 min-w-44 max-w-80 justify-between gap-2 text-xs"
                          onClick={() => setWorkspaceDialogOpen(true)}
                        >
                          <span className="truncate">
                            {selectedWorkspace?.label ?? "选择工作目录"}
                          </span>
                        </Button>
                      ) : (
                        <div className="text-foreground truncate text-xs">
                          {workspaceMetadataOfThread(threadRecord)
                            .workspace_name ?? "未设置"}
                        </div>
                      )}
                    </div>
                    <InputBox
                      className={cn("bg-background/5 w-full")}
                      isNewThread={isNewThread}
                      threadId={threadId}
                      autoFocus={isNewThread}
                      status={
                        thread.error
                          ? "error"
                          : thread.isLoading
                            ? "streaming"
                            : "ready"
                      }
                      context={settings.context}
                      disabled={
                        env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true" ||
                        isUploading
                      }
                      onContextChange={(context) =>
                        setSettings("context", context)
                      }
                      onFollowupsVisibilityChange={setShowFollowups}
                      onSubmit={handleSubmit}
                      onStop={handleStop}
                    />
                  </div>
                ) : (
                  <div
                    aria-hidden="true"
                    className={cn(
                      "bg-background/5 h-32 w-full rounded-2xl border",
                    )}
                  />
                )}
                <WorkspacePickerDialog
                  open={workspaceDialogOpen}
                  onOpenChange={setWorkspaceDialogOpen}
                  value={selectedWorkspace}
                  fallbackWorkspace={workspaces[0]}
                  onConfirm={setSelectedWorkspace}
                />
                {env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true" && (
                  <div className="text-muted-foreground/67 w-full translate-y-12 text-center text-xs">
                    {t.common.notAvailableInDemoMode}
                  </div>
                )}
              </div>
            </div>
          </main>
        </div>
      </ChatBox>
    </ThreadContext.Provider>
  );
}
