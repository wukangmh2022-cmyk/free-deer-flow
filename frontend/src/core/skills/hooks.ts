import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { enableSkill } from "./api";

import { loadSkills } from ".";
import type { Skill } from "./type";

const SKILLS_CACHE_KEY = "deerflow:desktop:skills-cache";

function readSkillsCache(): Skill[] | undefined {
  if (typeof window === "undefined") {
    return undefined;
  }
  try {
    const raw = window.sessionStorage.getItem(SKILLS_CACHE_KEY);
    if (!raw) {
      return undefined;
    }
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Skill[]) : undefined;
  } catch {
    return undefined;
  }
}

function writeSkillsCache(skills: Skill[] | undefined): void {
  if (typeof window === "undefined" || !skills) {
    return;
  }
  try {
    window.sessionStorage.setItem(SKILLS_CACHE_KEY, JSON.stringify(skills));
  } catch {
    // Ignore storage failures; this is only a UI cache.
  }
}

export function useSkills() {
  const query = useQuery({
    queryKey: ["skills"],
    queryFn: () => loadSkills(),
    initialData: readSkillsCache,
    placeholderData: (previousData) => previousData,
    staleTime: 10_000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    writeSkillsCache(query.data);
  }, [query.data]);

  return {
    skills: query.data ?? [],
    isLoading: query.isLoading,
    error: query.error,
  };
}

export function useEnableSkill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      skillName,
      enabled,
    }: {
      skillName: string;
      enabled: boolean;
    }) => {
      await enableSkill(skillName, enabled);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}
