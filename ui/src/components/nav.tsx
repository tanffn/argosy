"use client";

import {
  BookOpen,
  Bot,
  ClipboardList,
  FileText,
  Flag,
  Home,
  Inbox,
  MessageCircle,
  PieChart,
  ScrollText,
  Settings,
  Target,
  Users,
  Wallet,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentType, SVGProps } from "react";

import { LiveClock } from "@/components/live-clock";
import { cn } from "@/lib/utils";

interface NavTab {
  href: string;
  label: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
}

// Two logical groups, separated visually so the nav reads as
// "here's what I touch daily | here's what I check when I need to."
// URLs stay flat under / -- the grouping is purely visual. Tab order
// inside PRIMARY mirrors the typical session flow (Home glance ->
// Advisor for data entry -> Portfolio/Expenses to read state -> Plan
// for the draft -> Retirement for the verdict -> Decide -> Proposals
// to approve). INSPECTION is occasional-use surfaces.
const PRIMARY_TABS: NavTab[] = [
  { href: "/", label: "Home", Icon: Home },
  { href: "/advisor", label: "Advisor", Icon: MessageCircle },
  { href: "/portfolio", label: "Portfolio", Icon: PieChart },
  { href: "/expenses", label: "Expenses", Icon: Wallet },
  { href: "/plan", label: "Plan", Icon: ClipboardList },
  { href: "/retirement", label: "Retirement", Icon: Flag },
  { href: "/decide", label: "Decide", Icon: Target },
  { href: "/proposals", label: "Proposals", Icon: Inbox },
];

const INSPECTION_TABS: NavTab[] = [
  { href: "/argonaut", label: "Argonaut", Icon: Bot },
  { href: "/agents", label: "Agents", Icon: Users },
  { href: "/files", label: "Files", Icon: FileText },
  { href: "/audit", label: "Audit", Icon: ScrollText },
  { href: "/domain-kb", label: "Domain KB", Icon: BookOpen },
  { href: "/settings", label: "Settings", Icon: Settings },
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
        <ul className="flex items-center gap-1 flex-wrap">
          {PRIMARY_TABS.map((t) => (
            <NavLink key={t.href} tab={t} active={pathname === t.href} />
          ))}
          <li
            role="separator"
            aria-hidden
            className="h-5 w-px bg-muted-foreground/40 mx-3 self-center"
          />
          {INSPECTION_TABS.map((t) => (
            <NavLink key={t.href} tab={t} active={pathname === t.href} />
          ))}
        </ul>
        <div className="ml-auto">
          <LiveClock seconds={false} />
        </div>
      </nav>
    </header>
  );
}

function NavLink({ tab, active }: { tab: NavTab; active: boolean }) {
  const Icon = tab.Icon;
  return (
    <li>
      <Link
        href={tab.href}
        className={cn(
          "px-3 py-1.5 rounded-md text-sm transition-colors relative inline-flex items-center gap-1.5",
          active
            ? "bg-secondary text-foreground border-b-2 border-primary -mb-[2px]"
            : "hover:bg-secondary/60 text-muted-foreground",
        )}
      >
        <Icon
          className="h-3.5 w-3.5"
          aria-hidden
          suppressHydrationWarning
        />
        {tab.label}
      </Link>
    </li>
  );
}
