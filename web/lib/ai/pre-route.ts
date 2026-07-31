// Deterministic pre-routing (PLAN.md §5.3 rule 4).
//
// A handful of messages are not questions at all — they are commands, and we
// know exactly what they mean. Matching those here and invoking the tool
// directly skips the model entirely: no inference, no latency, and — the
// actual point — no exposure to the one decision §3.3 says small models are
// worst at, which is *whether* a message needs a tool and *which* tool that
// is.
//
// The bar for adding a pattern is high, because the failure modes are not
// symmetric. Missing a command costs one ordinary model turn, which is what
// would have happened anyway. Matching something that was really a question
// hijacks it: the user asks "why did the sync fail yesterday?" and gets a
// sync kicked off instead of an answer. So every pattern below is anchored to
// the *whole* normalised message, and anything that even looks
// interrogative is refused before matching starts.

/** The three tools reachable without the model. */
export type PreRoutedToolName =
  | "get_envelope_status"
  | "get_review_queue"
  | "sync_accounts";

export type PreRoute = {
  toolName: PreRoutedToolName;
  /**
   * Always empty. Only tools whose no-argument call is unambiguously right are
   * pre-routed — `search_transactions` and `get_spending_report` need
   * arguments parsed out of prose, and a mis-parsed argument is a wrong answer
   * delivered confidently, which is worse than a slow one.
   * `allocate_to_envelope` writes, and nothing that writes to a ledger is
   * triggered by pattern-matching a sentence.
   */
  input: Record<string, never>;
};

/**
 * Words that mean "this is a question or a complaint, not an order".
 *
 * Defence in depth rather than the primary guard — the anchored patterns
 * below already reject every phrase containing one of these, because a whole
 * message that matches `^review$` has no room for a "why" in it. It exists so
 * that loosening a pattern later cannot silently start eating questions, and
 * it is checked first because it is cheaper than the regexes.
 */
const INTERROGATIVE = new Set([
  "am",
  "are",
  "broke",
  "broken",
  "can",
  "cant",
  "could",
  "did",
  "didnt",
  "do",
  "does",
  "doesnt",
  "error",
  "errors",
  "explain",
  "fail",
  "failed",
  "failing",
  "fails",
  "how",
  "is",
  "isnt",
  "last",
  "next",
  "should",
  "stuck",
  "was",
  "were",
  "what",
  "whats",
  "when",
  "where",
  "whether",
  "which",
  "who",
  "why",
  "will",
  "wont",
  "would",
  "wrong",
  "yesterday",
]);

/** Longest pre-routable command is five words ("show me the review queue"). */
const MAX_WORDS = 6;

const PATTERNS: ReadonlyArray<readonly [RegExp, PreRoutedToolName]> = [
  // "sync", "sync now", "sync my accounts", "refresh the transactions"
  [/^(re)?sync( now)?$/, "sync_accounts"],
  [
    /^((re)?sync|refresh)( (my|the))? (accounts?|banks?|transactions?)( now)?$/,
    "sync_accounts",
  ],

  // "review", "review queue", "show me the review queue"
  [/^review( queue)?$/, "get_review_queue"],
  [
    /^(show|open|see|view|list)( me)?( the| my)? review( queue)?$/,
    "get_review_queue",
  ],

  // "envelopes", "budget", "show my envelopes", "budget status"
  [/^(envelopes?|budget)( status)?$/, "get_envelope_status"],
  [
    /^(show|open|see|view|list)( me)?( the| my)? (envelopes?|budget)( status)?$/,
    "get_envelope_status",
  ],
];

/**
 * Lower-case, unpunctuated, single-spaced.
 *
 * Apostrophes are dropped rather than kept so that "didn't" collapses onto
 * the "didnt" in the veto list; curly and straight quotes both have to go,
 * since a Mac will happily produce either.
 *
 * Whitespace is collapsed and trimmed *before* trailing punctuation is
 * stripped, because that punctuation is matched with `$`. Stripping first
 * meant "sync my accounts! " kept its "!" — the anchored patterns then all
 * missed, and a plain command silently fell through to the model. A trailing
 * space is not a thing a user can see, so it must not change what a message
 * does.
 */
function normalise(text: string): string {
  return text
    .toLowerCase()
    .replace(/[’']/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[.!,;:]+$/g, "")
    .trim();
}

/**
 * The tool this message unambiguously commands, or `null` to let the model
 * decide — which is always a safe answer.
 */
export function preRoute(text: string): PreRoute | null {
  const normalised = normalise(text);

  if (normalised.length === 0) {
    return null;
  }
  // A question mark is a question. Nothing after this point can override it.
  if (normalised.includes("?")) {
    return null;
  }

  const words = normalised.split(" ");
  if (words.length > MAX_WORDS) {
    return null;
  }
  if (words.some((word) => INTERROGATIVE.has(word))) {
    return null;
  }

  for (const [pattern, toolName] of PATTERNS) {
    if (pattern.test(normalised)) {
      return { input: {}, toolName };
    }
  }
  return null;
}

/** The structural shape of an incoming message, kept free of `ChatMessage` so this stays testable. */
export type PreRoutableMessage = {
  role: string;
  parts?: Array<{ type: string; text?: string }>;
};

/**
 * Pre-route a whole message, or decline.
 *
 * Declines on anything but a lone text part from the user. A message carrying
 * an attachment, a tool result, or several parts has context that a regex over
 * one string cannot see, and guessing at it is exactly the false positive this
 * module is built to avoid.
 */
export function preRouteMessage(
  message: PreRoutableMessage | undefined | null
): PreRoute | null {
  if (message?.role !== "user") {
    return null;
  }
  const parts = message.parts ?? [];
  if (parts.length !== 1) {
    return null;
  }
  const [part] = parts;
  if (part.type !== "text" || typeof part.text !== "string") {
    return null;
  }
  return preRoute(part.text);
}
