// Argosy Phase 6: NextAuth route protection.
// Protects all tenant-data routes; the dashboard and the onboarding
// page are the only public surfaces.

import { withAuth } from "next-auth/middleware";

export default withAuth({
  pages: {
    signIn: "/onboarding",
  },
});

export const config = {
  matcher: [
    "/portfolio/:path*",
    "/plan/:path*",
    "/proposals/:path*",
    "/argonaut/:path*",
    "/audit/:path*",
  ],
};
