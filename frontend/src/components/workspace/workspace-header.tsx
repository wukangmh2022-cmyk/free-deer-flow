"use client";

import { MessageSquarePlus } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { useI18n } from "@/core/i18n/hooks";
import { env } from "@/env";
import { cn } from "@/lib/utils";

export function WorkspaceHeader({ className }: { className?: string }) {
  const { t } = useI18n();
  const pathname = usePathname();
  const { open: isSidebarOpen } = useSidebar();

  return (
    <>
      <div
        className={cn(
          "group/workspace-header flex h-12 flex-col justify-center px-2",
          className,
        )}
      >
        <div
          className={cn(
            "flex items-center gap-2",
            isSidebarOpen ? "justify-between" : "justify-center",
          )}
        >
          <SidebarTrigger className="shrink-0 opacity-70" />
          {env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true" ? (
            <Link
              href="/"
              className={cn(
                "text-primary min-w-0 truncate font-serif text-2xl leading-none",
                !isSidebarOpen && "hidden",
              )}
            >
              DeerFlow
            </Link>
          ) : (
            <div
              className={cn(
                "text-primary min-w-0 cursor-default truncate font-serif text-2xl leading-none",
                !isSidebarOpen && "hidden",
              )}
            >
              DeerFlow
            </div>
          )}
        </div>
      </div>
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton
            isActive={pathname === "/workspace/chats/new"}
            asChild
            tooltip={t.sidebar.newChat}
          >
            <Link className="text-muted-foreground" href="/workspace/chats/new">
              <MessageSquarePlus size={16} />
              <span>{t.sidebar.newChat}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </>
  );
}
