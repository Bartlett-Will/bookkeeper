import { getCapabilities } from "@/lib/ai/models";

// Static — this build has exactly one local Ollama model, so there's no
// gateway model catalog to query. See lib/ai/models.ts.
export function GET() {
  const headers = {
    "Cache-Control": "public, max-age=86400, s-maxage=86400",
  };

  return Response.json(getCapabilities(), { headers });
}
