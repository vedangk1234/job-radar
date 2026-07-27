import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { setApplied } from "@/lib/jobs";

export const runtime = "nodejs"; // postgres.js needs real TCP sockets
export const dynamic = "force-dynamic";

/**
 * PATCH /api/jobs/applied  { canonical_id: string, applied: boolean }
 *
 * The only route in this app that writes to the `jobs` table, and it writes
 * exactly two columns — see setApplied in lib/jobs.ts for why those two are the
 * dashboard's to own while every other column on the table is the crawler's.
 *
 * WHY THE ID IS IN THE BODY AND NOT IN THE PATH
 * ---------------------------------------------
 * The sibling routes are `/api/companies/:id`, so `/api/jobs/:canonicalId` would
 * be the matching shape. It is not used because a canonical_id is not a number —
 * it is `{ats}:{company_id}:{native_id}` built out of a vendor's own identifier,
 * or `hash:` plus a sha256. Today none of the 1197 stored ids contains a slash,
 * a question mark or a hash character, but "today" is the whole problem: the
 * next ATS added could mint one, and an id with an encoded slash in it is a 404
 * from the router before any handler runs, which would look like a missing job
 * rather than a routing quirk. A body parameter has no such shape requirement.
 *
 * Authentication is requireSession(), the same first line every route here
 * starts with, so an unauthenticated PATCH is a 401 exactly like the others —
 * middleware would already have refused it, and this refuses it again locally so
 * the guarantee can be read off this file.
 *
 * The response carries the whole re-read row so the client renders what the
 * database now says rather than what the click assumed. That is the same
 * arrangement PATCH /api/companies/:id has with its optimistic switch, and it
 * matters here for one specific reason: `applied_at` is decided by the database
 * clock, so the browser cannot know it without being told.
 */
export async function PATCH(request: Request) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return badRequest("body was not JSON");
  }

  const canonicalId = typeof body.canonical_id === "string" ? body.canonical_id.trim() : "";
  if (!canonicalId) return badRequest("canonical_id must be a non-empty string");

  if (typeof body.applied !== "boolean") return badRequest("applied must be true or false");

  try {
    const job = await setApplied(canonicalId, body.applied);
    if (!job) return NextResponse.json({ error: "not_found" }, { status: 404 });
    return NextResponse.json({ job });
  } catch (err) {
    return serverError(err, "PATCH /api/jobs/applied");
  }
}
