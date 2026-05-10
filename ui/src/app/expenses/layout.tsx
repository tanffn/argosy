"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { FxToggle } from "@/components/expenses/fx-toggle";
import { cn } from "@/lib/utils";

const TABS = [
  { href: "/expenses", label: "Overview" },
  { href: "/expenses/transactions", label: "Transactions" },
  { href: "/expenses/sources", label: "Sources" },
  { href: "/expenses/trips", label: "Trips" },
  { href: "/expenses/rsu", label: "RSU" },
];

export default function ExpensesLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="max-w-6xl mx-auto p-6 flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Expenses</h1>
        <FxToggle />
      </div>
      <nav className="border-b border-border -mx-1">
        <ul className="flex items-center gap-1">
          {TABS.map((t) => {
            const active = pathname === t.href;
            return (
              <li key={t.href}>
                <Link
                  href={t.href}
                  className={cn(
                    "inline-block px-3 py-2 text-sm rounded-t-md transition-colors",
                    active
                      ? "bg-secondary text-foreground border-b-2 border-primary -mb-[2px]"
                      : "text-muted-foreground hover:text-foreground hover:bg-secondary/40",
                  )}
                >
                  {t.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      {children}
    </div>
  );
}
