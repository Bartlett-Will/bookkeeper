import { NextResponse } from "next/server";
import { auth } from "@/app/(auth)/auth";

// File uploads are out of scope for this build (PLAN.md §5.6 strips Vercel
// Blob and doesn't replace it with local-filesystem storage — "revisit only
// if receipt attachments are wanted later"). The attach button in the chat
// UI is left in place; this route answers honestly rather than pretending
// to succeed, and the client already surfaces `error` as a toast.
export async function POST(_request: Request) {
  const session = await auth();

  if (!session) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  return NextResponse.json(
    { error: "File uploads are not supported in this build." },
    { status: 501 }
  );
}
