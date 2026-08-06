import type { SupabaseClient } from "@supabase/supabase-js";

import { sql } from "./db";
import type { ResumeKind } from "./resume-text";

/**
 * The resume, in its two halves: a row in `resumes` and an object in the
 * private `resumes` bucket.
 *
 * WHY THE STORAGE CALLS TAKE A CLIENT INSTEAD OF MAKING ONE. Every function here
 * that touches the bucket is handed the caller's Supabase client, which carries
 * the caller's JWT. That is not dependency-injection for its own sake — it is
 * the security model. This app has no service-role key (lib/env.ts, and
 * lib/account.ts argues for the absence at length), so there is no privileged
 * client to construct: the ONLY way to reach an object is as the person who owns
 * it, and the storage policies in db/schema.sql are what decide whether they do.
 * A function here that built its own client would be a function that had to
 * choose an identity, and the whole point is that it never gets to.
 *
 * WHICH ID GOES WHERE, and this is the distinction the whole slice turns on:
 *
 *     resumes.user_id   public.users.id  — what every foreign key here points at
 *     bucket path       auth.users.id    — what auth.uid() returns in a policy
 *
 * They are equal for every account created since 2a and NOT equal for the
 * Phase-1 account, whose id predates auth entirely (see lib/account.ts). Key the
 * object on the wrong one and that person uploads into a folder their own policy
 * cannot match — a 403 on their own file, which reads as a broken bucket rather
 * than as a mismatched id. lib/guard.Session carries both; callers pass both.
 */

export const BUCKET = "resumes";

export type ResumeRow = {
  bucket_path: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  uploaded_at: string;
};

/**
 * Where this person's resume lives. One object per person, named for its type.
 *
 * DETERMINISTIC RATHER THAN UNIQUE, deliberately. A timestamped or random name
 * would make every upload a new object and leave the old one behind unless
 * something remembered to delete it — and "something remembered" is how a bucket
 * fills with the resumes of people who thought they had replaced theirs. One
 * path per person per type means replacing a PDF with a PDF is an overwrite that
 * cannot orphan anything at all.
 *
 * The type is in the name because it is the one thing that can change, and that
 * is the single case an overwrite does not cover: a person replacing a PDF with
 * a .docx writes a new object and the old one has to be removed explicitly. That
 * is what removeStaleObject below is for, and why the erasure path lists the
 * folder instead of trusting bucket_path.
 */
export function resumePath(authUserId: string, kind: ResumeKind): string {
  return `${authUserId}/resume.${kind}`;
}

/** This person's stored resume, or null. */
export async function loadResume(userId: string): Promise<ResumeRow | null> {
  const [row] = await sql<ResumeRow[]>`
    SELECT bucket_path, filename, content_type, size_bytes, uploaded_at
      FROM resumes
     WHERE user_id = ${userId}
  `;
  return row ?? null;
}

/**
 * Upload the bytes, then record them. In that order, and it is not arbitrary.
 *
 * A row written first and an upload that then fails is a profile claiming a
 * resume that does not exist — every read of it 404s, and the person is told
 * they have a file they cannot see or replace. The other way round, a successful
 * upload whose row fails to write leaves an object nobody has a pointer to,
 * which the next upload overwrites (same deterministic path) and which erasure
 * removes anyway (it lists the folder). One of those states is invisible and
 * self-correcting; the other is a broken profile. So: bytes first.
 *
 * upsert: true is what makes "replace" work without a delete-then-write window
 * in which the person has no resume at all.
 */
export async function saveResume(
  supabase: SupabaseClient,
  {
    userId,
    authUserId,
    kind,
    filename,
    contentType,
    bytes,
    size,
  }: {
    userId: string;
    authUserId: string;
    kind: ResumeKind;
    filename: string;
    contentType: string;
    bytes: Uint8Array;
    size: number;
  },
): Promise<ResumeRow> {
  const path = resumePath(authUserId, kind);

  const { error } = await supabase.storage.from(BUCKET).upload(path, bytes, {
    contentType,
    upsert: true,
  });
  if (error) {
    throw new StorageWriteError(
      `the resume could not be stored (${error.message})`,
    );
  }

  const [row] = await sql<ResumeRow[]>`
    INSERT INTO resumes (user_id, bucket_path, filename, content_type, size_bytes, uploaded_at)
    VALUES (${userId}, ${path}, ${filename}, ${contentType}, ${size}, now())
    ON CONFLICT (user_id) DO UPDATE
       SET bucket_path  = EXCLUDED.bucket_path,
           filename     = EXCLUDED.filename,
           content_type = EXCLUDED.content_type,
           size_bytes   = EXCLUDED.size_bytes,
           uploaded_at  = now()
    RETURNING bucket_path, filename, content_type, size_bytes, uploaded_at
  `;
  return row;
}

export class StorageWriteError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StorageWriteError";
  }
}

/**
 * After a replace ACROSS types, remove the object the old row named.
 *
 * Only that case: replacing a PDF with a PDF overwrites one object and there is
 * nothing stale to remove. Best-effort by design — the new resume is already
 * stored and the row already points at it, so failing the whole request because
 * a superseded file lingered would turn a successful upload into an error.
 * Nothing is lost by tolerating it either: the object is inside the person's own
 * folder, and erasure removes the FOLDER rather than the recorded path, so a
 * leftover cannot outlive the account. It is logged so the tolerance is visible.
 */
export async function removeStaleObject(
  supabase: SupabaseClient,
  previousPath: string | null | undefined,
  currentPath: string,
): Promise<void> {
  if (!previousPath || previousPath === currentPath) return;
  const { error } = await supabase.storage.from(BUCKET).remove([previousPath]);
  if (error) {
    console.error(
      `[resume] replaced ${previousPath} with ${currentPath} but could not remove the ` +
        `old object: ${error.message}. It is inside the owner's folder and will go ` +
        `with the account.`,
    );
  }
}

/**
 * A short-lived URL for the person's own file.
 *
 * SIGNED, AND SHORT. The bucket is private, so there is no URL that works
 * without one; sixty seconds is long enough for the browser to follow a redirect
 * and short enough that a link pasted somewhere is dead before it is read. The
 * `download` parameter restores the person's own filename, which the
 * deterministic object name has thrown away.
 *
 * Returns null rather than throwing when the policy refuses. A refusal here is
 * not an exception — it is the correct answer to "may I read this", and the one
 * the isolation proof asks for.
 */
export async function signedResumeUrl(
  supabase: SupabaseClient,
  path: string,
  filename: string,
): Promise<string | null> {
  const { data, error } = await supabase.storage
    .from(BUCKET)
    .createSignedUrl(path, 60, { download: filename });
  if (error || !data) {
    console.error(`[resume] could not sign ${path}: ${error?.message ?? "no url returned"}`);
    return null;
  }
  return data.signedUrl;
}

/**
 * The storage half of erasure, as the narrow interface lib/account.ts needs.
 *
 * lib/account.ts takes this rather than a SupabaseClient so it keeps importing
 * no Supabase and no next/headers — it is a database module, it is the most
 * destructive code in the app, and it stays unit-testable. app/actions.ts is
 * where the real client is built and handed over.
 */
export type ResumeStorage = {
  /** Every object path inside this person's folder. */
  list(authUserId: string): Promise<string[]>;
  /** Remove these paths. Resolves to the error message, or null on success. */
  remove(paths: string[]): Promise<string | null>;
};

export function resumeStorage(supabase: SupabaseClient): ResumeStorage {
  return {
    async list(authUserId) {
      const { data, error } = await supabase.storage.from(BUCKET).list(authUserId);
      if (error) throw new Error(`could not list the resume folder: ${error.message}`);
      return (data ?? []).map((entry) => `${authUserId}/${entry.name}`);
    },
    async remove(paths) {
      if (!paths.length) return null;
      const { error } = await supabase.storage.from(BUCKET).remove(paths);
      return error ? error.message : null;
    },
  };
}
