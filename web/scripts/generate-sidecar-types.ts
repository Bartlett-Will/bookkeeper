/**
 * Regenerates `lib/sidecar/types.ts` from the running sidecar's OpenAPI schema.
 *
 *   cd sidecar && uv run bookkeeper serve   # in one terminal
 *   cd web && pnpm run sidecar:types        # in another
 *
 * PLAN.md §5.1 justifies the sidecar partly on "FastAPI emits OpenAPI; we
 * generate TS types from it, so the tool layer is type-checked end to end
 * rather than passing untyped JSON". This script is what makes that a
 * property rather than a claim.
 *
 * The output is committed. A build must never require a running sidecar, and
 * CI has no Python process — but more importantly, a checked-in file makes
 * schema drift show up as a reviewable diff instead of as a runtime shape
 * error in the middle of a chat turn.
 *
 * Paths resolve against the working directory, which `pnpm run` pins to
 * `web/`. Invoke it through the package script rather than directly.
 */

import { writeFile } from "node:fs/promises";
import path from "node:path";
import openapiTS, { astToString } from "openapi-typescript";

const SIDECAR_BASE_URL =
  process.env.SIDECAR_BASE_URL ?? "http://127.0.0.1:8000";
const OUTPUT_PATH = path.resolve(process.cwd(), "lib/sidecar/types.ts");

const BANNER = `/**
 * GENERATED FILE — DO NOT EDIT BY HAND.
 *
 * Produced from the sidecar's OpenAPI schema by \`pnpm run sidecar:types\`
 * (see scripts/generate-sidecar-types.ts). Hand-editing this file
 * reintroduces exactly the drift PLAN.md §5.1's typed contract exists to
 * prevent: change the pydantic models in \`sidecar/bookkeeper/api.py\`, then
 * regenerate.
 */

`;

async function main() {
  const schemaUrl = new URL("/openapi.json", SIDECAR_BASE_URL);

  let contents: string;
  try {
    contents = astToString(await openapiTS(schemaUrl));
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    process.stderr.write(
      `Could not read ${schemaUrl.href}: ${reason}\n` +
        "Start the sidecar first: cd sidecar && uv run bookkeeper serve\n"
    );
    process.exitCode = 1;
    return;
  }

  await writeFile(OUTPUT_PATH, BANNER + contents, "utf8");
  process.stdout.write(
    `Wrote ${path.relative(process.cwd(), OUTPUT_PATH)} from ${schemaUrl.href}\n` +
      "Run `pnpm run fix` to format it, then commit the result.\n"
  );
}

main().catch((error: unknown) => {
  process.stderr.write(`${error instanceof Error ? error.stack : error}\n`);
  process.exitCode = 1;
});
