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
};

export default nextConfig;
