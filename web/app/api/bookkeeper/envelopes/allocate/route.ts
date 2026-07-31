import type { NextRequest } from "next/server";
import { allocateToEnvelope } from "@/lib/sidecar/client";
import type { AllocateRequest } from "@/lib/sidecar/contract";
import {
  readJsonBody,
  respondWithBadRequest,
  respondWithSidecarResult,
} from "@/lib/sidecar/respond";

/**
 * Moves money into an envelope by appending a `custom "envelope" "allocate"`
 * directive. The one write tool of PLAN.md §5.3.
 *
 * `amount` is a decimal *string* and stays one all the way to the ledger.
 * Pydantic serializes `Decimal` as a string precisely so no float rounding is
 * ever applied to someone's money; parsing it to a `number` here — even
 * transiently, to "validate" it — would reintroduce that. The check below is
 * therefore textual.
 */

// Anchored, no exponent, at most two fractional digits: the shape a ledger
// amount can take. Rejecting here keeps a typo from becoming a directive.
const DECIMAL_STRING = /^-?\d+(\.\d{1,2})?$/;

// The sidecar's own default for `AllocateRequest.currency`. Restated rather
// than omitted because the generated type marks it required; if the ledger
// ever operates in something other than USD, this is the line that changes.
const DEFAULT_CURRENCY = "USD";

function validate(body: unknown): AllocateRequest | string {
  if (typeof body !== "object" || body === null) {
    return "expected a JSON object";
  }
  const { allocated_on, amount, currency, envelope } = body as Record<
    string,
    unknown
  >;

  if (typeof envelope !== "string" || envelope.trim() === "") {
    return "expected `envelope` to be a non-empty string";
  }
  if (typeof amount !== "string" || !DECIMAL_STRING.test(amount)) {
    return 'expected `amount` to be a decimal string such as "125.00"';
  }
  if (
    allocated_on !== undefined &&
    allocated_on !== null &&
    typeof allocated_on !== "string"
  ) {
    return "expected `allocated_on` to be an ISO date string when present";
  }
  if (currency !== undefined && typeof currency !== "string") {
    return "expected `currency` to be a string when present";
  }

  return {
    amount,
    currency: currency ?? DEFAULT_CURRENCY,
    envelope,
    ...(allocated_on === undefined ? {} : { allocated_on }),
  };
}

export async function POST(request: NextRequest) {
  const parsed = await readJsonBody(request);
  if (!parsed.ok) {
    return respondWithBadRequest("request body was not valid JSON");
  }

  const validated = validate(parsed.value);
  if (typeof validated === "string") {
    return respondWithBadRequest(validated);
  }

  return respondWithSidecarResult(
    await allocateToEnvelope(validated, { signal: request.signal })
  );
}
