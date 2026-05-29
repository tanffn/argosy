import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      // Preserve the /api/ prefix on the backend side. FastAPI mounts most
      // routers at /api/* and a few infra endpoints at root (/health,
      // /internal/*). The frontend always uses the /api/* path; this proxy
      // forwards as-is so backend route shapes don't have to change.
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
    ];
  },
  async redirects() {
    return [
      // /decide → /consult rename (2026-05-29). The tab is a ticker
      // consultation surface (decisions still happen on /proposals);
      // the old name was misleading. Permanent 308 keeps old bookmarks
      // and external links working. Remove the redirect after one sprint
      // cycle once external references catch up.
      {
        source: "/decide",
        destination: "/consult",
        permanent: true,
      },
      {
        source: "/decide/:path*",
        destination: "/consult/:path*",
        permanent: true,
      },
    ];
  },
};

export default nextConfig;
