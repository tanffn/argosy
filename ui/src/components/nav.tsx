"use client";

import {
  Bell,
  BookOpen,
  Bot,
  ChevronDown,
  CircleHelp,
  ClipboardList,
  FileText,
  Flag,
  Gavel,
  Home,
  Inbox,
  MessageCircle,
  PieChart,
  ScrollText,
  Settings,
  Users,
  Wallet,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ComponentType, type SVGProps } from "react";

import { LiveClock } from "@/components/live-clock";
import { cn } from "@/lib/utils";

interface NavTab {
  href: string;
  label: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
}

// PRIMARY tabs are visible at all times -- daily-to-monthly use,
// ordered by typical session flow (Home glance -> Advisor for data
// entry -> Portfolio/Expenses to read state -> Plan for the draft
// -> Retirement for the verdict -> Consult on a ticker -> Proposals
// to approve).
// Proposals sits immediately after Portfolio: the action hub lives next to the
// state it acts on (read your portfolio -> act on it). Consult is NOT a primary
// tab — it's folded into the Proposals hub as "Ask the team"; the /consult
// route stays as a working deep link. Exported so the ordering invariant is
// unit-testable.
export const PRIMARY_TABS: NavTab[] = [
  { href: "/", label: "Home", Icon: Home },
  { href: "/advisor", label: "Advisor", Icon: MessageCircle },
  { href: "/portfolio", label: "Portfolio", Icon: PieChart },
  { href: "/proposals", label: "Proposals", Icon: Inbox },
  { href: "/expenses", label: "Expenses", Icon: Wallet },
  { href: "/plan", label: "Plan", Icon: ClipboardList },
  { href: "/retirement", label: "Retirement", Icon: Flag },
];

// INSPECTION tabs live behind a "More" dropdown -- occasional use
// (monthly check-in / debugging / reference / setup-only) so cluttering
// the top row with them costs more than it saves.
const INSPECTION_TABS: NavTab[] = [
  { href: "/argonaut", label: "Argonaut", Icon: Bot },
  { href: "/agents", label: "Agents", Icon: Users },
  { href: "/decisions", label: "Decisions", Icon: Gavel },
  { href: "/files", label: "Files", Icon: FileText },
  { href: "/audit", label: "Audit", Icon: ScrollText },
  { href: "/domain-kb", label: "Domain KB", Icon: BookOpen },
  { href: "/settings", label: "Settings", Icon: Settings },
  // Spec E commit #7 — push-subscription card + channel x severity x kind
  // preference matrix. Sibling of /settings under the same inspection group.
  { href: "/settings/notifications", label: "Notifications", Icon: Bell },
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
          {/* eslint-disable-next-line @next/next/no-img-element -- static brand
              mark; using a plain <img> avoids the next/image dependency
              + lets us swap the asset (ui/public/logo.png) without a
              rebuild. */}
          <img
            src="/logo.png"
            alt=""
            className="h-7 w-7 rounded-sm"
            aria-hidden
          />
          <span className="font-mono font-semibold tracking-tight text-lg text-foreground">
            Argosy
          </span>
        </Link>
        <ul className="flex items-center gap-1 flex-wrap">
          {PRIMARY_TABS.map((t) => (
            <NavLink key={t.href} tab={t} active={pathname === t.href} />
          ))}
          <MoreMenu tabs={INSPECTION_TABS} pathname={pathname} />
        </ul>
        <div className="ml-auto flex items-center gap-3">
          <a
            href="/user-guide/index.html"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center justify-center h-7 w-7 rounded-md text-muted-foreground hover:bg-secondary/60 hover:text-foreground transition-colors"
            aria-label="Open user guide in a new tab"
            title="User guide"
          >
            <CircleHelp className="h-4 w-4" aria-hidden suppressHydrationWarning />
          </a>
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

interface MoreMenuProps {
  tabs: NavTab[];
  pathname: string;
}

function MoreMenu({ tabs, pathname }: MoreMenuProps) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLLIElement | null>(null);

  // One of the inspection tabs is currently routed -- pulse the More
  // button as "active" so the user knows where they are without
  // having to open the menu.
  const childActive = tabs.some((t) => t.href === pathname);

  // Close on outside-click. Mousedown (not click) so a click on a menu
  // item still routes correctly before the close fires.
  useEffect(() => {
    if (!open) return;
    function handleMouseDown(ev: MouseEvent) {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(ev.target as Node)) setOpen(false);
    }
    function handleKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  return (
    <li ref={wrapperRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className={cn(
          "px-3 py-1.5 rounded-md text-sm transition-colors relative inline-flex items-center gap-1.5",
          childActive
            ? "bg-secondary text-foreground border-b-2 border-primary -mb-[2px]"
            : "hover:bg-secondary/60 text-muted-foreground",
        )}
      >
        <Settings className="h-3.5 w-3.5" aria-hidden suppressHydrationWarning />
        More
        <ChevronDown
          className={cn(
            "h-3 w-3 transition-transform",
            open ? "rotate-180" : "",
          )}
          aria-hidden
        />
      </button>
      {open ? (
        <div
          role="menu"
          className="absolute left-0 top-full mt-1 min-w-[180px] rounded-md border border-border bg-background/95 backdrop-blur shadow-md py-1 z-20"
        >
          {tabs.map((t) => {
            const Icon = t.Icon;
            const active = pathname === t.href;
            return (
              <Link
                key={t.href}
                href={t.href}
                role="menuitem"
                onClick={() => setOpen(false)}
                className={cn(
                  "flex items-center gap-2 px-3 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-secondary text-foreground"
                    : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
                )}
              >
                <Icon
                  className="h-3.5 w-3.5"
                  aria-hidden
                  suppressHydrationWarning
                />
                {t.label}
              </Link>
            );
          })}
        </div>
      ) : null}
    </li>
  );
}
