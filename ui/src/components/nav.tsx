"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const TABS = [
  { href: "/", label: "Home" },
  { href: "/portfolio", label: "Portfolio" },
  { href: "/plan", label: "Plan" },
  { href: "/proposals", label: "Proposals" },
];

export function NavBar() {
  const pathname = usePathname();
  return (
    <header className="border-b border-border bg-background/60 backdrop-blur sticky top-0 z-10">
      <nav className="max-w-6xl mx-auto px-6 py-3 flex items-center gap-6">
        <span className="font-semibold tracking-tight text-lg">Argosy</span>
        <ul className="flex items-center gap-2">
          {TABS.map((t) => {
            const active = pathname === t.href;
            return (
              <li key={t.href}>
                <Link
                  href={t.href}
                  className={cn(
                    "px-3 py-1.5 rounded-md text-sm transition-colors",
                    active
                      ? "bg-secondary text-secondary-foreground"
                      : "hover:bg-secondary/60 text-muted-foreground",
                  )}
                >
                  {t.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
