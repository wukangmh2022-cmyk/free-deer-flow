"use client";

import { Loader2Icon } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useProviderAuthStatus } from "@/core/provider-auth/hooks";

export function ProviderAuthGuard({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const pathname = usePathname();
  const router = useRouter();
  const statusQuery = useProviderAuthStatus({
    refetchInterval: false,
    refetchOnWindowFocus: true,
  });

  const hasAnyReady = statusQuery.data?.hasAnyReady ?? false;

  useEffect(() => {
    if (statusQuery.isLoading) {
      return;
    }
    if (!hasAnyReady) {
      const target = pathname ? `/?from=${encodeURIComponent(pathname)}` : "/";
      router.replace(target);
    }
  }, [hasAnyReady, pathname, router, statusQuery.isLoading]);

  if (statusQuery.isLoading || !hasAnyReady) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="flex items-center gap-3 rounded-full border border-border/60 bg-background/90 px-5 py-3 text-sm text-muted-foreground shadow-sm">
          <Loader2Icon className="size-4 animate-spin" />
          正在检查 Provider 登录状态...
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
