// NextAuth v4 handler for Argosy (Phase 6).
//
// Credentials provider for the first-time login flow: a setup token
// (issued by `argosy admin tenant create`) is exchanged for a session.
// In production additional providers (Google / GitHub) can be enabled
// here without changing the rest of the app.

import NextAuth, { NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const authOptions: NextAuthOptions = {
  session: { strategy: "jwt" },
  secret: process.env.NEXTAUTH_SECRET,
  pages: {
    signIn: "/onboarding",
  },
  providers: [
    CredentialsProvider({
      name: "Setup token",
      credentials: {
        email: { label: "Email", type: "email" },
        token: { label: "Setup token", type: "text" },
      },
      async authorize(credentials) {
        if (!credentials?.email || !credentials.token) return null;
        // Exchange the setup token for a tenant binding via the engine.
        const r = await fetch(
          `${API_URL}/api/onboarding/redeem`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              email: credentials.email,
              token: credentials.token,
            }),
          }
        ).catch(() => null);
        if (!r || !r.ok) return null;
        const body = await r.json();
        if (!body?.user_id) return null;
        return {
          id: String(body.user_id),
          email: String(credentials.email),
          name: String(body.user_id),
        };
      },
    }),
  ],
  callbacks: {
    async jwt({ token, user }) {
      if (user?.email) {
        token.email = user.email;
        token.user_id = user.id;
      }
      return token;
    },
    async session({ session, token }) {
      if (session.user && token.email) {
        session.user.email = token.email as string;
        (session.user as Record<string, unknown>).user_id = token.user_id;
      }
      return session;
    },
  },
};

const handler = NextAuth(authOptions);

export { handler as GET, handler as POST };
