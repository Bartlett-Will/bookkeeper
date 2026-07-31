export const isProductionEnvironment = process.env.NODE_ENV === "production";
export const isDevelopmentEnvironment = process.env.NODE_ENV === "development";
export const isTestEnvironment = Boolean(
  process.env.PLAYWRIGHT_TEST_BASE_URL ||
    process.env.PLAYWRIGHT ||
    process.env.CI_PLAYWRIGHT
);

/**
 * The first-run prompts. This is the only place the app tells a new user what
 * it is for, so each one maps to a different tool of PLAN.md §5.3 — between
 * them they name four of the six, and none of them describes something the
 * assistant cannot do.
 *
 * Two are phrased as the bare commands that `lib/ai/pre-route.ts` matches, so
 * the first thing a user clicks answers instantly and without an inference
 * (§5.3 rule 4). The other two deliberately need the model, so tool selection
 * is exercised early rather than on the first question that matters.
 */
export const suggestions = [
  "Sync my accounts",
  "Show me the review queue",
  "How am I doing on groceries?",
  "Where did my money go over the last three months?",
];
