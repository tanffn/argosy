import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { LiveClock } from "@/components/live-clock";
import { NavBar } from "@/components/nav";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Argosy",
  description: "Argosy: multi-agent financial advisor",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Dark mode default per SDD §11.2; light tokens still defined for opt-in.
  // suppressHydrationWarning: needed because some browser extensions
  // (notably Dark Reader) inject `data-darkreader-*` attributes into <html>
  // before React hydrates, which would otherwise produce a hydration mismatch
  // warning. Suppression scoped to <html> only — not children.
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased dark`}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <NavBar />
        <div className="flex-1">{children}</div>
        <footer className="border-t border-border mt-8">
          <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between text-xs text-muted-foreground gap-4 flex-wrap">
            <span className="font-mono">
              Argosy v0.1.0 · multi-agent financial advisor
            </span>
            <LiveClock label="Last updated" />
          </div>
        </footer>
      </body>
    </html>
  );
}
