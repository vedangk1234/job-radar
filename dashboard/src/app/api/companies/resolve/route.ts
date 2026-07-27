import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { resolve, tokenVariants } from "@/lib/ats";
import { sql } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
// Up to 18 sequential probes (6 token variants x 3 boards). Rarely near this,
// but the ceiling has to be honest or Vercel will cut a slow one off later.
export const maxDuration = 60;

/**
 * Add-by-name, step 1: guess the board and report what was found. WRITES
 * NOTHING — the human confirms against the sample titles, and POST
 * /api/companies is what stores it.
 *
 * The split matters. `resolve()` is a heuristic over guessed tokens, and it can
 * confidently return the wrong company: a token guessed from "navi" may well
 * belong to somebody else's board with real jobs on it. Showing five job titles
 * and waiting is the difference between a lookup and a lucky guess.
 */
export async function POST(request: Request) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return badRequest("body was not JSON");
  }

  const name = String(body.name ?? "").trim();
  if (!name) return badRequest("name is required");
  if (name.length > 120) return badRequest("that name is too long to be a company name");

  try {
    const found = await resolve(name);

    if (!found) {
      const variants = tokenVariants(name);
      return NextResponse.json({
        found: false,
        tried: variants,
        message:
          `No greenhouse, lever or ashby board answered for ${variants.length} guessed ` +
          `token(s). That is a real answer, not a failure: plenty of companies use a ` +
          `token no mechanical rule produces. Paste the board URL instead.`,
      });
    }

    // Already in the table? Say so before offering to "add" it.
    const [existing] = await sql<{ id: number; name: string; tracked: boolean }[]>`
      SELECT id, name, tracked FROM companies
       WHERE ats = ${found.ats} AND token = ${found.token}
    `;

    return NextResponse.json({
      found: true,
      candidate: {
        name,
        ats: found.ats,
        token: found.token,
        config: {},
        category: "unknown",
        india_scoped: false,
      },
      jobCount: found.jobCount,
      sampleTitles: found.sampleTitles,
      probesUsed: found.tried,
      existing: existing ?? null,
    });
  } catch (err) {
    return serverError(err, "POST /api/companies/resolve");
  }
}
