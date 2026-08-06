import assert from "node:assert/strict";
import { test } from "node:test";
import { returnPathFor } from "../proxy";

/**
 * The proxy redirects an unauthenticated request through auto-sign-in and
 * back. It used to rebuild the return path from `pathname` alone, so the
 * query string was silently dropped.
 *
 * That is worth a test rather than a comment because of *how* it failed:
 * `/api/bookkeeper/review-queue?limit=5` came back as the entire queue, a
 * well-formed 200 that simply answered a different question than the one
 * asked. Browsers never see it — they hold a cookie after the first request
 * and stop being redirected — so it only affects scripts, cron sync, and
 * measurement harnesses, which are precisely the callers whose output gets
 * trusted without a human reading it.
 */

test("keeps the query string on the return path", () => {
  assert.equal(
    returnPathFor("http://localhost:3000/api/bookkeeper/review-queue?limit=5"),
    "/api/bookkeeper/review-queue?limit=5"
  );
});

test("keeps every parameter, not just the first", () => {
  assert.equal(
    returnPathFor(
      "http://localhost:3000/api/bookkeeper/reports/spending?from=2026-01-01&to=2026-03-31"
    ),
    "/api/bookkeeper/reports/spending?from=2026-01-01&to=2026-03-31"
  );
});

test("preserves encoding in parameter values", () => {
  // `q` carries free text straight from a chat message; a search for
  // "whole foods" must not arrive as "whole" with the rest lost.
  assert.equal(
    returnPathFor(
      "http://localhost:3000/api/bookkeeper/transactions/search?q=whole%20foods"
    ),
    "/api/bookkeeper/transactions/search?q=whole%20foods"
  );
});

test("returns a bare path unchanged when there is no query", () => {
  assert.equal(
    returnPathFor("http://localhost:3000/api/bookkeeper/verify"),
    "/api/bookkeeper/verify"
  );
});

test("drops the origin, so the result stays a relative path", () => {
  // The sign-in route refuses anything not starting with a single "/", which
  // is what stops this becoming an open redirect. Returning an absolute URL
  // here would silently send every caller to "/" instead.
  const path = returnPathFor(
    "http://evil.example.com/api/bookkeeper/verify?x=1"
  );
  assert.ok(path.startsWith("/"));
  assert.ok(!path.startsWith("//"));
  assert.equal(path, "/api/bookkeeper/verify?x=1");
});
