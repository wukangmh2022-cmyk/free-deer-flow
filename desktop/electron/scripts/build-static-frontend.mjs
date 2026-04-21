import fs from "fs";
import path from "path";
import { spawnSync } from "child_process";

const repoRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const frontendSourceDir = path.join(repoRoot, "frontend");
const workspaceDir = path.join(repoRoot, "desktop", "electron", ".desktop-static", "frontend");
const outputDir = path.join(repoRoot, "desktop", "electron", "runtime", "frontend-static");

function rmrf(target) {
  fs.rmSync(target, { recursive: true, force: true });
}

function ensureDir(target) {
  fs.mkdirSync(target, { recursive: true });
}

function writeFile(target, content) {
  ensureDir(path.dirname(target));
  fs.writeFileSync(target, content);
}

function replaceDynamicPageWithWrapper(pagePath, importName, paramsCode) {
  const fullPath = path.join(workspaceDir, pagePath);
  const original = fs.readFileSync(fullPath, "utf8");
  const clientPath = fullPath.replace(/page\.tsx$/, "page.client.tsx");
  fs.writeFileSync(clientPath, original);
  fs.writeFileSync(
    fullPath,
    `import { Suspense } from "react";
import ${importName} from "./page.client";

export const dynamicParams = false;

export function generateStaticParams() {
  return [${paramsCode}];
}

export default function Page() {
  return (
    <Suspense fallback={null}>
      <${importName} />
    </Suspense>
  );
}
`,
  );
}

rmrf(workspaceDir);
rmrf(outputDir);

fs.cpSync(frontendSourceDir, workspaceDir, {
  recursive: true,
  filter(source) {
    const relative = path.relative(frontendSourceDir, source);
    if (!relative) return true;
    if (
      relative === ".next" ||
      relative.startsWith(`.next${path.sep}`) ||
      relative === "node_modules" ||
      relative.startsWith(`node_modules${path.sep}`) ||
      relative === "out" ||
      relative.startsWith(`out${path.sep}`)
    ) {
      return false;
    }
    return true;
  },
});

try {
  fs.symlinkSync(
    path.join(frontendSourceDir, "node_modules"),
    path.join(workspaceDir, "node_modules"),
    "dir",
  );
} catch (error) {
  if (error && error.code !== "EEXIST") {
    throw error;
  }
}

rmrf(path.join(workspaceDir, "src", "app", "api"));
rmrf(path.join(workspaceDir, "src", "app", "mock", "api"));
rmrf(path.join(workspaceDir, "src", "app", "[lang]"));

writeFile(
  path.join(workspaceDir, "src", "app", "page.tsx"),
  `import { Header } from "@/components/landing/header";
import { Hero } from "@/components/landing/hero";
import { DEFAULT_LOCALE } from "@/core/i18n";

export default function LandingPage() {
  return (
    <div className="bg-background text-foreground min-h-screen w-full">
      <Header
        minimal
        className="bg-background/88 supports-[backdrop-filter]:bg-background/72"
        homeURL="/"
        locale={DEFAULT_LOCALE}
      />
      <main className="flex w-full flex-col">
        <Hero />
      </main>
    </div>
  );
}
`,
);

writeFile(
  path.join(workspaceDir, "src", "app", "layout.tsx"),
  `import "@/styles/globals.css";
import "katex/dist/katex.min.css";

import { type Metadata } from "next";

import { ThemeProvider } from "@/components/theme-provider";
import { ThemeSync } from "@/components/theme-sync";
import { I18nProvider } from "@/core/i18n/context";
import { DEFAULT_LOCALE } from "@/core/i18n";

export const metadata: Metadata = {
  title: "DeerFlow",
  description: "A LangChain-based framework for building super agents.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang={DEFAULT_LOCALE}
      suppressContentEditableWarning
      suppressHydrationWarning
    >
      <body>
        <ThemeProvider attribute="class" enableSystem disableTransitionOnChange>
          <ThemeSync />
          <I18nProvider initialLocale={DEFAULT_LOCALE}>{children}</I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
`,
);

writeFile(
  path.join(workspaceDir, "src", "app", "workspace", "layout.tsx"),
  `"use client";

import { useState } from "react";
import { Toaster } from "sonner";

import { QueryClientProvider } from "@/components/query-client-provider";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { CommandPalette } from "@/components/workspace/command-palette";
import { WorkspaceSidebar } from "@/components/workspace/workspace-sidebar";

function parseSidebarOpenCookie(value: string | undefined): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

function getSidebarCookieValue() {
  if (typeof document === "undefined") return undefined;
  const entry = document.cookie
    .split("; ")
    .find((part) => part.startsWith("sidebar_state="));
  return entry?.split("=")[1];
}

export default function WorkspaceLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const [initialSidebarOpen] = useState<boolean | undefined>(() =>
    parseSidebarOpenCookie(getSidebarCookieValue()),
  );

  return (
    <QueryClientProvider>
      <SidebarProvider className="h-screen" defaultOpen={initialSidebarOpen}>
        <WorkspaceSidebar />
        <SidebarInset className="min-w-0">{children}</SidebarInset>
      </SidebarProvider>
      <CommandPalette />
      <Toaster position="top-center" />
    </QueryClientProvider>
  );
}
`,
);

writeFile(
  path.join(workspaceDir, "src", "app", "workspace", "page.tsx"),
  `"use client";

import { useEffect } from "react";

export default function WorkspacePage() {
  useEffect(() => {
    window.location.replace("/workspace/chats/new");
  }, []);

  return null;
}
`,
);

replaceDynamicPageWithWrapper(
  path.join("src", "app", "workspace", "chats", "[thread_id]", "page.tsx"),
  "WorkspaceChatPageClient",
  `{ thread_id: "__desktop__" }, { thread_id: "new" }`,
);

replaceDynamicPageWithWrapper(
  path.join(
    "src",
    "app",
    "workspace",
    "agents",
    "[agent_name]",
    "chats",
    "[thread_id]",
    "page.tsx",
  ),
  "WorkspaceAgentChatPageClient",
  `{ agent_name: "__desktop_agent__", thread_id: "__desktop_thread__" }`,
);

const buildEnv = {
  ...process.env,
  DEER_FLOW_DESKTOP_STATIC: "1",
  BETTER_AUTH_SECRET:
    process.env.BETTER_AUTH_SECRET || "desktop-local-placeholder-secret-1234567890",
  NEXT_PUBLIC_BACKEND_BASE_URL:
    process.env.NEXT_PUBLIC_BACKEND_BASE_URL || "",
  NEXT_PUBLIC_LANGGRAPH_BASE_URL:
    process.env.NEXT_PUBLIC_LANGGRAPH_BASE_URL || "/api/langgraph-compat",
  NEXT_PUBLIC_STATIC_WEBSITE_ONLY: "false",
};

const nextBin = path.join(
  frontendSourceDir,
  "node_modules",
  "next",
  "dist",
  "bin",
  "next",
);

const result = spawnSync(process.execPath, [nextBin, "build", "--webpack"], {
  cwd: workspaceDir,
  env: {
    ...buildEnv,
    NODE_PATH: path.join(frontendSourceDir, "node_modules"),
  },
  stdio: "inherit",
});

if (result.status !== 0) {
  process.exit(result.status ?? 1);
}

fs.cpSync(path.join(workspaceDir, "out"), outputDir, { recursive: true });
