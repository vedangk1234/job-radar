import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { loadResume, removeStaleObject, resumePath, saveResume, StorageWriteError } from "@/lib/resume";
import { parseResume, ResumeError, STATUS_FOR } from "@/lib/resume-text";
import { extractSkills } from "@/lib/skills";
import { KEYWORD_CAP } from "@/lib/labels";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Upload or replace this person's resume, and suggest keywords from it.
 *
 * ONE REQUEST DOES ALL OF IT — validate, parse, extract, store — rather than the
 * more usual shape of a direct-to-storage upload from the browser followed by a
 * "now go read it" call. Two reasons, and the first is the requirement:
 * validation has to happen on the server, and a browser that has already written
 * the object has already stored an unvalidated file. Second, the two-call
 * version has a failure between the calls — an object uploaded and never
 * registered — which is a state somebody then has to write reconciliation for.
 *
 * WHAT IS STORED AND WHAT IS NOT. The FILE goes into the private bucket. The
 * extracted TEXT is parsed in this request and thrown away in it — there is no
 * column for it anywhere, exactly as there is no column for a job description.
 * The suggested keywords are RETURNED, not saved: they are a proposal, the
 * person edits them, and PUT /api/preferences is what writes the result. So a
 * successful upload changes `resumes` and the bucket, and changes nothing about
 * what any digest does.
 *
 * THE UPLOAD TRAVELS ON THE CALLER'S OWN JWT. createClient() carries their
 * cookies, so the storage policies in db/schema.sql decide whether the write
 * lands. Nothing in this file checks whose folder is being written to, because
 * nothing in this file gets to choose it: resumePath() is built from the auth id
 * on the verified session, and the policy re-derives the same thing from
 * auth.uid(). Two independent statements of the same fact, one of them in
 * Postgres.
 */
export async function POST(request: Request) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;
  const { userId, authUserId } = gate.session;

  let file: File | null = null;
  try {
    const form = await request.formData();
    const field = form.get("file");
    if (field instanceof File) file = field;
  } catch {
    return badRequest("the upload could not be read as a form submission");
  }
  if (!file) {
    return badRequest("no file was attached to the upload");
  }

  try {
    // Everything that can refuse the file refuses it here, before a byte is
    // stored. A resume that cannot be read is not worth keeping: we would be
    // holding somebody's personal data in order to show them an error about it.
    const parsed = await parseResume(file);

    const previous = await loadResume(userId);
    const supabase = await createClient();

    const row = await saveResume(supabase, {
      userId,
      authUserId,
      kind: parsed.kind,
      filename: safeFilename(file.name, parsed.kind),
      contentType: parsed.contentType,
      bytes: parsed.bytes,
      size: parsed.size,
    });

    await removeStaleObject(
      supabase,
      previous?.bucket_path,
      resumePath(authUserId, parsed.kind),
    );

    const keywords = extractSkills(parsed.text, KEYWORD_CAP);

    return NextResponse.json({
      resume: row,
      keywords,
      message:
        keywords.length > 0
          ? `Read ${row.filename}. ${keywords.length} skill${keywords.length === 1 ? "" : "s"} found — ` +
            `remove what does not fit and add anything we missed.`
          : `Read ${row.filename}, but none of the skills in it matched our list. ` +
            `Type the ones you want to be alerted about.`,
    });
  } catch (err) {
    if (err instanceof ResumeError) {
      // A refused file is not a server fault and is not logged as one. The
      // message is written for the person who chose the file, so it is the
      // response body rather than a correlation id to quote at somebody.
      return NextResponse.json(
        { error: err.code, detail: err.message },
        { status: STATUS_FOR[err.code] },
      );
    }
    if (err instanceof StorageWriteError) {
      // The file was fine and we could not keep it. That is ours, so it says so
      // — and it says the resume was NOT saved, because the alternative is a
      // person believing they have uploaded something they have not.
      console.error("[POST /api/resume] storage write failed", err);
      return NextResponse.json(
        {
          error: "storage_failed",
          detail:
            "Your resume could not be saved just now, so nothing was stored. " +
            "Try again in a moment.",
        },
        { status: 502 },
      );
    }
    return serverError(err, "POST /api/resume");
  }
}

/**
 * The name shown on screen and used for the download, cleaned.
 *
 * It is display text and a Content-Disposition value, never a path — the object
 * name comes from resumePath() and owes nothing to this. Still cleaned: a
 * filename arrives from the client, it is rendered in the browser and echoed in
 * a header, and both of those are places a 400-character name with a newline in
 * it causes a problem that has nothing to do with resumes.
 */
function safeFilename(raw: string, kind: string): string {
  const base = (raw || `resume.${kind}`)
    .replace(/[\r\n\t]+/g, " ")
    .replace(/[/\\]/g, "-")
    .trim();
  return (base || `resume.${kind}`).slice(0, 120);
}
