import NextAuth, { type DefaultSession } from "next-auth";
import type { DefaultJWT } from "next-auth/jwt";
import Credentials from "next-auth/providers/credentials";
import { getOrCreateLocalUser } from "@/lib/db/queries";
import { authConfig } from "./auth.config";

// Single-user, localhost-only app: there is no login flow, just one seeded
// local user (see lib/db/queries.ts#getOrCreateLocalUser). "regular" is kept
// as the only UserType so entitlements and session shape stay unchanged from
// upstream instead of ripping out the type across the codebase.
export type UserType = "regular";

declare module "next-auth" {
  interface Session extends DefaultSession {
    user: {
      id: string;
      type: UserType;
    } & DefaultSession["user"];
  }

  interface User {
    email?: string | null;
    id?: string;
    type: UserType;
  }
}

declare module "next-auth/jwt" {
  interface JWT extends DefaultJWT {
    id: string;
    type: UserType;
  }
}

export const {
  handlers: { GET, POST },
  auth,
  signIn,
  signOut,
} = NextAuth({
  ...authConfig,
  callbacks: {
    jwt({ token, user }) {
      if (user) {
        token.id = user.id as string;
        token.type = user.type;
      }

      return token;
    },
    session({ session, token }) {
      if (session.user) {
        session.user.id = token.id;
        session.user.type = token.type;
      }

      return session;
    },
  },
  providers: [
    Credentials({
      // No credentials are collected or checked. This provider exists only
      // so the proxy (app/proxy.ts) can auto-sign-in as the single local
      // user via /api/auth/local — see app/(auth)/api/auth/local/route.ts.
      async authorize() {
        const localUser = await getOrCreateLocalUser();
        return { ...localUser, type: "regular" };
      },
      credentials: {},
      id: "local",
    }),
  ],
});
