import { NextResponse } from "next/server";

import { requireSession, serverError } from "@/lib/guard";
import { loadResume, signedResumeUrl } from "@/lib/resume";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Open this person's own resume, via a URL that stops working in a minute.
 *
 * IT TAKES NO PARAMETERS, AND THAT IS THE DESIGN. There is no path, no id and no
 * query string — the only resume this route can produce is the one belonging to
 * the session making the request, because the row is looked up BY the session's
 * user id and the signature is minted with the session's own JWT. A route that
 * accepted a path would be a route whose safety depended on validating it, and
 * the validation would be a second copy of the rule the storage policy already
 * enforces.
 *
 * TWO INDEPENDENT REFUSALS PROTECT IT, which is why the isolation proof can be
 * run against this route rather than only against raw storage. Asking for
 * somebody else's resume is not possible to express here; and if it somehow
 * were, createSignedUrl runs as the caller and the policy in db/schema.sql
 * refuses a folder that is not theirs. The app-level check could be deleted
 * tomorrow and the Postgres one would still hold.
 *
 * A REDIRECT RATHER THAN A PROXY. Streaming the bytes through this function
 * would put a 5 MB file through a serverless invocation for no benefit — the
 * signed URL is already unguessable and already expiring, and Supabase serves it
 * faster than we can relay it.
 */
export async function GET() {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  try {
    const resume = await loadResume(gate.session.userId);
    if (!resume) {
      return NextResponse.json(
        { error: "no_resume", detail: "You have not uploaded a resume yet." },
        { status: 404 },
      );
    }

    const supabase = await createClient();
    const url = await signedResumeUrl(supabase, resume.bucket_path, resume.filename);
    if (!url) {
      // The row says there is a file and storage would not sign for it. Reported
      // as a real failure rather than as "no resume": the two have completely
      // different causes and only one of them is the person's to fix.
      return NextResponse.json(
        {
          error: "unavailable",
          detail:
            "Your resume is recorded but could not be opened just now. " +
            "Try again, or upload it again to replace it.",
        },
        { status: 502 },
      );
    }

    return NextResponse.redirect(url, 302);
  } catch (err) {
    return serverError(err, "GET /api/resume/download");
  }
}
