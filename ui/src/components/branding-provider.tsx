// Argosy Phase 6: branding context provider.
//
// Wraps children, fetches per-tenant branding once on mount, and
// surfaces the resolved branding via React context for nav / layout
// components.

"use client";

import { createContext, useContext } from "react";
import { useBranding, DEFAULT_BRANDING, type Branding } from "@/lib/branding";

const BrandingContext = createContext<Branding>(DEFAULT_BRANDING);

export function BrandingProvider({
  userId,
  children,
}: {
  userId: string | null;
  children: React.ReactNode;
}) {
  const branding = useBranding(userId);
  return (
    <BrandingContext.Provider value={branding}>
      {children}
    </BrandingContext.Provider>
  );
}

export function useBrandingContext(): Branding {
  return useContext(BrandingContext);
}
