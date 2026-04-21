"use client";

import { useEffect } from "react";

import { useLocalSettings } from "@/core/settings";

const THEME_ATTRIBUTE = "data-color-theme";

export function ThemeSync() {
  const [settings] = useLocalSettings();

  useEffect(() => {
    document.documentElement.setAttribute(
      THEME_ATTRIBUTE,
      settings.appearance.color_theme,
    );
  }, [settings.appearance.color_theme]);

  return null;
}
