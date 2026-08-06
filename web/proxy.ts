import { type NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";
import { isDevelopmentEnvironment } from "./lib/constants";

/**
 * The path to return to after auto-sign-in, query string included.
 *
 * Exported so the query-preservation property is testable without standing
 * up next-auth. Dropping `search` made the first unauthenticated call to
 * `/api/bookkeeper/review-queue?limit=5` come back as the *whole* queue —
 * the limit vanished and the response still looked plausible, which is the
 * bad kind of wrong. A browser never sees it, since it gets a cookie on the
 * first request and stops being redirected; it only bites non-browser
 * callers like scripts, cron sync, and measurement harnesses, which are
 * exactly the callers whose numbers get believed.
 */
export function returnPathFor(url: string): string {
  const { pathname, search } = new URL(url);
  return `${pathname}${search}`;
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname.startsWith("/ping")) {
    return new Response("pong", { status: 200 });
  }

  if (pathname.startsWith("/api/auth")) {
    return NextResponse.next();
  }

  const token = await getToken({
    req: request,
    secret: process.env.AUTH_SECRET,
    secureCookie: !isDevelopmentEnvironment,
  });

  const base = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

  if (!token) {
    // Single-user, localhost-only app: no login page, just auto-sign-in as
    // the one seeded local user (see app/(auth)/api/auth/local/route.ts).
    const redirectUrl = encodeURIComponent(returnPathFor(request.url));

    return NextResponse.redirect(
      new URL(`${base}/api/auth/local?redirectUrl=${redirectUrl}`, request.url)
    );
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    "/",
    "/chat/:id",
    "/api/:path*",

    "/((?!_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt).*)",
  ],
};
