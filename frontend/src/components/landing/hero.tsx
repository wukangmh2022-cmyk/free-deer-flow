"use client";

import { ProviderEntryPanel } from "@/components/landing/provider-entry-panel";
import { cn } from "@/lib/utils";

export function Hero({ className }: { className?: string }) {
  return (
    <section
      className={cn(
        "bg-background text-foreground",
        className,
      )}
    >
      <div className="container-md mx-auto flex min-h-[calc(100vh-4rem)] items-center justify-center px-4 py-16">
        <div className="w-full max-w-xl">
          <ProviderEntryPanel />
        </div>
      </div>
    </section>
  );
}
