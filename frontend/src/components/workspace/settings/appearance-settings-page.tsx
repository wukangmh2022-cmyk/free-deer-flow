"use client";

import {
  MonitorSmartphoneIcon,
  MoonIcon,
  PaletteIcon,
  SunIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { useMemo, type ComponentType, type SVGProps } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { enUS, isLocale, zhCN, type Locale } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";
import { useLocalSettings } from "@/core/settings";
import type { AppearanceColorTheme } from "@/core/settings/local";
import { cn } from "@/lib/utils";

import { SettingsSection } from "./settings-section";

const languageOptions: { value: Locale; label: string }[] = [
  { value: "en-US", label: enUS.locale.localName },
  { value: "zh-CN", label: zhCN.locale.localName },
];

export function AppearanceSettingsPage() {
  const { t, locale, changeLocale } = useI18n();
  const { theme, setTheme, systemTheme } = useTheme();
  const [settings, setSettings] = useLocalSettings();
  const currentTheme = (theme ?? "system") as "system" | "light" | "dark";
  const currentColorTheme = settings.appearance.color_theme;

  const themeOptions = useMemo(
    () => [
      {
        id: "system",
        label: t.settings.appearance.system,
        description: t.settings.appearance.systemDescription,
        icon: MonitorSmartphoneIcon,
      },
      {
        id: "light",
        label: t.settings.appearance.light,
        description: t.settings.appearance.lightDescription,
        icon: SunIcon,
      },
      {
        id: "dark",
        label: t.settings.appearance.dark,
        description: t.settings.appearance.darkDescription,
        icon: MoonIcon,
      },
    ],
    [
      t.settings.appearance.dark,
      t.settings.appearance.darkDescription,
      t.settings.appearance.light,
      t.settings.appearance.lightDescription,
      t.settings.appearance.system,
      t.settings.appearance.systemDescription,
    ],
  );

  const colorThemeOptions = useMemo(
    () => [
      {
        id: "warm" as const,
        label: t.settings.appearance.warm,
        description: t.settings.appearance.warmDescription,
        primaryClassName: "bg-amber-700",
        backgroundClassName: "bg-amber-50",
        sidebarClassName: "bg-stone-100",
      },
      {
        id: "stone" as const,
        label: t.settings.appearance.stone,
        description: t.settings.appearance.stoneDescription,
        primaryClassName: "bg-slate-600",
        backgroundClassName: "bg-slate-50",
        sidebarClassName: "bg-zinc-100",
      },
      {
        id: "blue" as const,
        label: t.settings.appearance.blue,
        description: t.settings.appearance.blueDescription,
        primaryClassName: "bg-blue-600",
        backgroundClassName: "bg-blue-50",
        sidebarClassName: "bg-sky-100",
      },
    ],
    [
      t.settings.appearance.blue,
      t.settings.appearance.blueDescription,
      t.settings.appearance.stone,
      t.settings.appearance.stoneDescription,
      t.settings.appearance.warm,
      t.settings.appearance.warmDescription,
    ],
  );

  return (
    <div className="space-y-8">
      <SettingsSection
        title={t.settings.appearance.themeTitle}
        description={t.settings.appearance.themeDescription}
      >
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {themeOptions.map((option) => (
            <ThemePreviewCard
              key={option.id}
              icon={option.icon}
              label={option.label}
              description={option.description}
              active={currentTheme === option.id}
              mode={option.id as "system" | "light" | "dark"}
              systemTheme={systemTheme}
              onSelect={(value) => setTheme(value)}
            />
          ))}
        </div>
      </SettingsSection>

      <Separator />

      <SettingsSection
        title={t.settings.appearance.colorThemeTitle}
        description={t.settings.appearance.colorThemeDescription}
      >
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {colorThemeOptions.map((option) => (
            <ColorThemePreviewCard
              key={option.id}
              icon={PaletteIcon}
              label={option.label}
              description={option.description}
              active={currentColorTheme === option.id}
              colorTheme={option.id}
              primaryClassName={option.primaryClassName}
              backgroundClassName={option.backgroundClassName}
              sidebarClassName={option.sidebarClassName}
              onSelect={(value) =>
                setSettings("appearance", { color_theme: value })
              }
            />
          ))}
        </div>
      </SettingsSection>

      <Separator />

      <SettingsSection
        title={t.settings.appearance.languageTitle}
        description={t.settings.appearance.languageDescription}
      >
        <Select
          value={locale}
          onValueChange={(value) => {
            if (isLocale(value)) {
              changeLocale(value);
            }
          }}
        >
          <SelectTrigger className="w-[220px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {languageOptions.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </SettingsSection>
    </div>
  );
}

function ThemePreviewCard({
  icon: Icon,
  label,
  description,
  active,
  mode,
  systemTheme,
  onSelect,
}: {
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  label: string;
  description: string;
  active: boolean;
  mode: "system" | "light" | "dark";
  systemTheme?: string;
  onSelect: (mode: "system" | "light" | "dark") => void;
}) {
  const previewMode =
    mode === "system" ? (systemTheme === "dark" ? "dark" : "light") : mode;
  return (
    <button
      type="button"
      onClick={() => onSelect(mode)}
      aria-pressed={active}
      className={cn(
        "group flex h-full min-w-0 flex-col gap-3 overflow-hidden rounded-lg border p-4 text-left transition-all",
        active
          ? "border-primary ring-primary/30 shadow-sm ring-2"
          : "hover:border-border hover:shadow-sm",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="bg-muted rounded-md p-2">
          <Icon className="size-4" />
        </div>
        <div className="space-y-1">
          <div className="text-sm leading-none font-semibold">{label}</div>
          <p className="text-muted-foreground text-xs leading-snug">
            {description}
          </p>
        </div>
      </div>
      <div
        className={cn(
          "relative h-[118px] w-full overflow-hidden rounded-xl border text-xs transition-colors",
          previewMode === "dark"
            ? "border-white/10 bg-neutral-950 text-neutral-200"
            : "border-slate-200 bg-white text-slate-900",
        )}
      >
        <div className="flex h-full min-w-0">
          <div
            className={cn(
              "flex w-16 shrink-0 flex-col gap-2 border-r px-2 py-2",
              previewMode === "dark"
                ? "border-white/10 bg-white/5"
                : "border-slate-200 bg-slate-50/90",
            )}
          >
            <div className="h-2 w-9 rounded-full bg-current/14" />
            <div className="space-y-1">
              <div className="h-1.5 w-7 rounded-full bg-current/10" />
              <div className="h-1.5 w-8 rounded-full bg-current/10" />
              <div className="h-1.5 w-6 rounded-full bg-current/10" />
            </div>
          </div>
          <div className="min-w-0 flex-1 space-y-2 p-2.5">
            <div className="flex items-center gap-2">
              <div
                className={cn(
                  "h-2.5 w-2.5 rounded-full",
                  previewMode === "dark" ? "bg-emerald-400" : "bg-emerald-500",
                )}
              />
              <div className="h-2 w-12 rounded-full bg-current/20" />
              <div className="h-2 w-7 rounded-full bg-current/12" />
              <div className="ml-auto h-5 w-10 rounded-full bg-current/10" />
            </div>
            <div
              className={cn(
                "rounded-md border p-2",
                previewMode === "dark"
                  ? "border-white/10 bg-white/5"
                  : "border-slate-200 bg-slate-50/90",
              )}
            >
              <div className="space-y-1.5">
                <div className="h-2 w-3/4 rounded-full bg-current/15" />
                <div className="h-2 w-1/2 rounded-full bg-current/10" />
                <div className="flex gap-1.5 pt-1">
                  <div className="h-5 w-12 rounded-md bg-current/90" />
                  <div className="h-5 w-9 rounded-md bg-current/12" />
                </div>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div
                className={cn(
                  "rounded-md border p-2",
                  previewMode === "dark"
                    ? "border-white/10 bg-white/5"
                    : "border-slate-200 bg-slate-50/80",
                )}
              >
                <div className="space-y-1.5">
                  <div className="h-2 w-5/6 rounded-full bg-current/15" />
                  <div className="h-2 w-2/3 rounded-full bg-current/10" />
                </div>
              </div>
              <div
                className={cn(
                  "rounded-md border p-2",
                  previewMode === "dark"
                    ? "border-white/10 bg-white/5"
                    : "border-slate-200 bg-slate-50/80",
                )}
              >
                <div className="space-y-1.5">
                  <div className="h-2 w-2/3 rounded-full bg-current/15" />
                  <div className="h-2 w-1/2 rounded-full bg-current/10" />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </button>
  );
}

function ColorThemePreviewCard({
  icon: Icon,
  label,
  description,
  active,
  colorTheme,
  primaryClassName,
  backgroundClassName,
  sidebarClassName,
  onSelect,
}: {
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  label: string;
  description: string;
  active: boolean;
  colorTheme: AppearanceColorTheme;
  primaryClassName: string;
  backgroundClassName: string;
  sidebarClassName: string;
  onSelect: (theme: AppearanceColorTheme) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(colorTheme)}
      aria-pressed={active}
      className={cn(
        "group flex h-full min-w-0 flex-col gap-3 overflow-hidden rounded-lg border p-4 text-left transition-all",
        active
          ? "border-primary ring-primary/30 shadow-sm ring-2"
          : "hover:border-border hover:shadow-sm",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="bg-muted rounded-md p-2">
          <Icon className="size-4" />
        </div>
        <div className="space-y-1">
          <div className="text-sm leading-none font-semibold">{label}</div>
          <p className="text-muted-foreground text-xs leading-snug">
            {description}
          </p>
        </div>
      </div>
      <div
        className={cn(
          "h-[112px] w-full overflow-hidden rounded-xl border border-slate-200/80",
          backgroundClassName,
        )}
      >
        <div className="grid h-full grid-cols-[72px_1fr]">
          <div className={cn("border-r border-black/5 px-2.5 py-2", sidebarClassName)}>
            <div className="space-y-2">
              <div className="h-2.5 w-10 rounded-full bg-black/10" />
              <div className="space-y-1">
                <div className="h-1.5 w-8 rounded-full bg-black/8" />
                <div className="h-1.5 w-10 rounded-full bg-black/8" />
                <div className="h-1.5 w-7 rounded-full bg-black/8" />
              </div>
            </div>
          </div>
          <div className="min-w-0 space-y-2 p-2.5">
            <div className="flex items-center gap-2">
              <div className={cn("h-2.5 w-2.5 rounded-full", primaryClassName)} />
              <div className="h-2 w-14 rounded-full bg-black/10" />
              <div className="ml-auto h-5 w-10 rounded-full bg-white/90 shadow-sm ring-1 ring-black/5" />
            </div>
            <div className="h-2.5 w-2/3 rounded-full bg-black/12" />
            <div className="rounded-md bg-white/88 p-2 shadow-sm ring-1 ring-black/5">
              <div className="space-y-2">
                <div className="h-2 w-4/5 rounded-full bg-black/10" />
                <div className="h-2 w-3/5 rounded-full bg-black/7" />
                <div className="mt-2 flex gap-1.5">
                  <div
                    className={cn(
                      "h-5 w-12 rounded-md shadow-sm",
                      primaryClassName,
                    )}
                  />
                  <div className="h-5 w-9 rounded-md bg-black/6" />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </button>
  );
}
