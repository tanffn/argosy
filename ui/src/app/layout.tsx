import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

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
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased dark`}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <NavBar />
        <div className="flex-1">{children}</div>
      </body>
    </html>
  );
}
