"use client";

import {
  Loader2Icon,
  LogInIcon,
  MoveRightIcon,
  RefreshCcwIcon,
} from "lucide-react";
import { useEffect } from "react";
import { toast } from "sonner";
import { Toaster } from "sonner";

import { QueryClientProvider } from "@/components/query-client-provider";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import {
  useOpenProviderLogin,
  useProviderAuthStatus,
} from "@/core/provider-auth/hooks";

function ProviderEntryPanelInner() {
  const statusQuery = useProviderAuthStatus();
  const deepseekLogin = useOpenProviderLogin("deepseek");
  const xiaomiLogin = useOpenProviderLogin("xiaomi");

  const providers = statusQuery.data?.providers;
  const hasAnyReady = statusQuery.data?.hasAnyReady ?? false;
  const isRefreshing = statusQuery.isFetching && !statusQuery.isLoading;

  useEffect(() => {
    if (!providers) {
      return;
    }
    document.cookie = `provider_auth_ready_deepseek=${providers.deepseek.ready ? "true" : "false"}; path=/; max-age=300; samesite=lax`;
    document.cookie = `provider_auth_ready_xiaomi=${providers.xiaomi.ready ? "true" : "false"}; path=/; max-age=300; samesite=lax`;
  }, [providers]);

  const handleOpenLogin = async (
    provider: "deepseek" | "xiaomi",
    label: string,
  ) => {
    const mutation = provider === "deepseek" ? deepseekLogin : xiaomiLogin;
    try {
      await mutation.mutateAsync();
      toast(`${label} 登录窗口已打开，请完成登录后点击刷新状态`);
      await statusQuery.refetch();
    } catch (error) {
      const message =
        error instanceof Error ? error.message : `打开 ${label} 登录失败`;
      toast(message);
    }
  };

  const openWorkspace = () => {
    if (!hasAnyReady) {
      toast("请先登录 DeepSeek 或 Xiaomi");
      return;
    }
    window.location.assign("/workspace");
  };

  return (
    <Card className="bg-card text-card-foreground border-border/70 w-full py-0 shadow-[0_16px_48px_color-mix(in_oklab,var(--foreground)_10%,transparent)]">
      <CardContent className="px-5 py-3 md:px-6">
        <div className="divide-border/70 divide-y">
          <ProviderRow
          label={providers?.deepseek.label ?? "DeepSeek"}
          ready={providers?.deepseek.ready ?? false}
          detail={
            providers?.deepseek.ready
              ? "检测到可复用的登录状态"
              : "尚未检测到本地会话或 cookie"
          }
          loading={deepseekLogin.isPending}
          onLogin={() => void handleOpenLogin("deepseek", "DeepSeek")}
        />
          <ProviderRow
          label={providers?.xiaomi.label ?? "Xiaomi MiMo"}
          ready={providers?.xiaomi.ready ?? false}
          detail={
            providers?.xiaomi.ready
              ? "检测到可复用的登录状态"
              : "尚未检测到本地会话或 cookie"
          }
          loading={xiaomiLogin.isPending}
          onLogin={() => void handleOpenLogin("xiaomi", "Xiaomi MiMo")}
        />
        </div>
      </CardContent>
      <CardFooter className="border-border/70 flex flex-col items-stretch gap-3 border-t px-5 py-4 md:flex-row md:items-center md:justify-between md:px-6">
        <div className="text-muted-foreground text-sm">
          {statusQuery.isLoading
            ? "正在检测本地登录状态..."
            : hasAnyReady
              ? "至少一个账号已就绪，可以进入工作区。"
              : "请先登录任一账号。"}
        </div>
        <div className="flex flex-col gap-3 sm:flex-row">
          <Button
            type="button"
            variant="outline"
            onClick={() => void statusQuery.refetch()}
            disabled={statusQuery.isLoading || isRefreshing}
          >
            {isRefreshing ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              <RefreshCcwIcon className="size-4" />
            )}
            刷新状态
          </Button>
          <Button
            type="button"
            onClick={openWorkspace}
            disabled={!hasAnyReady || statusQuery.isLoading}
          >
            进入工作区
            <MoveRightIcon className="size-4" />
          </Button>
        </div>
      </CardFooter>
    </Card>
  );
}

function ProviderRow({
  label,
  ready,
  detail,
  loading,
  onLogin,
}: {
  label: string;
  ready: boolean;
  detail: string;
  loading: boolean;
  onLogin: () => void;
}) {
  return (
    <div className="flex flex-col gap-4 py-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-3">
          <div className="text-lg font-semibold tracking-tight">{label}</div>
          <div
            className={
              ready
                ? "rounded-full border border-emerald-300/70 bg-emerald-500/10 px-3 py-1 text-sm font-medium text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/15 dark:text-emerald-300"
                : "bg-secondary text-secondary-foreground border-border/70 rounded-full border px-3 py-1 text-sm font-medium"
            }
          >
            {ready ? "已就绪" : "待登录"}
          </div>
        </div>
        <div className="text-muted-foreground text-sm leading-6">
          {detail}
        </div>
      </div>
      <Button
        type="button"
        variant="outline"
        className="w-full sm:w-auto"
        onClick={onLogin}
        disabled={loading}
      >
        {loading ? (
          <Loader2Icon className="size-4 animate-spin" />
        ) : (
          <LogInIcon className="size-4" />
        )}
        登录 {label}
      </Button>
    </div>
  );
}

export function ProviderEntryPanel() {
  return (
    <QueryClientProvider>
      <ProviderEntryPanelInner />
      <Toaster position="top-center" />
    </QueryClientProvider>
  );
}

export function ProviderEntryActions() {
  return (
    <QueryClientProvider>
      <ProviderEntryPanelInner />
      <Toaster position="top-center" />
    </QueryClientProvider>
  );
}
