import { useMutation, useQuery } from "@tanstack/react-query";

import { loadProviderAuthStatus, openProviderLogin, type ProviderKey } from "./api";

export function useProviderAuthStatus({
  refetchInterval,
  refetchOnWindowFocus = true,
}: {
  refetchInterval?: number | false;
  refetchOnWindowFocus?: boolean;
} = {}) {
  return useQuery({
    queryKey: ["provider-auth-status"],
    queryFn: loadProviderAuthStatus,
    staleTime: 5_000,
    refetchInterval,
    refetchOnWindowFocus,
  });
}

export function useOpenProviderLogin(provider: ProviderKey) {
  return useMutation({
    mutationKey: ["provider-auth-open-login", provider],
    mutationFn: () => openProviderLogin(provider),
  });
}
