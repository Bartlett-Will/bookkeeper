import type { NextAuthConfig } from "next-auth";

const base = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export const authConfig = {
  basePath: "/api/auth",
  callbacks: {},
  pages: {
    // No login page exists (single local user, auto-authenticated by
    // app/proxy.ts via /api/auth/local) — send NextAuth here on any
    // internal redirect instead of a nonexistent /login route.
    newUser: `${base}/`,
    signIn: `${base}/`,
  },
  providers: [],
  trustHost: true,
} satisfies NextAuthConfig;
