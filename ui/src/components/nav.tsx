"use client";

import { MessageCircle } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentType, SVGProps } from "react";

import { LiveClock } from "@/components/live-clock";
import { StatusPill } from "@/components/ui/status-pill";
import { cn } from "@/lib/utils";

interface NavTab {
  href: string;
  label: string;
  Icon?: ComponentType<SVGProps<SVGSVGElement>>;
}

// Advisor sits second — promoted from buried last-tab to a primary
// surface so the gap-tracker / Q&A panel is one click from any page.
const TABS: NavTab[] = [
  { href: "/", label: "Home" },
  { href: "/advisor", label: "Advisor", Icon: MessageCircle },
  { href: "/portfolio", label: "Portfolio" },
  { href: "/plan", label: "Plan" },
  { href: "/proposals", label: "Proposals" },
  { href: "/argonaut", label: "Argonaut" },
  { href: "/agents", label: "Agents" },
  { href: "/audit", label: "Audit" },
  { href: "/domain-kb", label: "Domain KB" },
  { href: "/settings", label: "Settings" },
];

export function NavBar() {
  const pathname = usePathname();
  return (
    <header className="border-b border-border bg-background/60 backdrop-blur sticky top-0 z-10">
      <nav className="max-w-6xl mx-auto px-6 py-3 flex items-center gap-4 flex-wrap">
        <Link
          href="/"
          className="flex items-center gap-2 shrink-0"
          aria-label="Argosy home"
        >
          <span className="font-mono text-lg leading-none" aria-hidden>
            🚢
          </span>
          <span className="font-mono font-semibold tracking-tight text-lg text-foreground">
            Argosy
          </span>
        </Link>
        <StatusPill tone="neutral" mono>
          v0.1.0
        </StatusPill>
        <ul className="flex items-center gap-1 flex-wrap">
          {TABS.map((t) => {
            const active = pathname === t.href;
            const Icon = t.Icon;
            return (
              <li key={t.href}>
                <Link
                  href={t.href}
                  className={cn(
                    "px-3 py-1.5 rounded-md text-sm transition-colors relative inline-flex items-center gap-1.5",
                    active
                      ? "bg-secondary text-foreground border-b-2 border-primary -mb-[2px]"
                      : "hover:bg-secondary/60 text-muted-foreground",
                  )}
                >
                  {Icon ? (
                    <Icon className="h-3.5 w-3.5" aria-hidden />
                  ) : null}
                  {t.label}
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="ml-auto">
          <LiveClock seconds={false} />
        </div>
      </nav>
    </header>
  );
}
