import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { matchingJobsForCompany, openJobsForCompany } from "@/lib/jobs";
import { loadPreferences } from "@/lib/preferences";

export const runtime = "nodejs"; // postgres.js needs real TCP sockets
export const dynamic = "force-dynamic";

/**
 * GET /api/companies/:id/jobs?filter=matches|all — the jobs behind one expanded row.
 *
 * The two screens differ by exactly this parameter, which is the whole design:
 * Home passes `matches` and gets the digest filter applied; Jobs passes `all`
 * and gets every open posting including the sales roles and the VP openings.
 * One route, one component, one query shape, and no second definition of "open".
 *
 * Read-only. There is no PATCH, POST or DELETE in this file and there should
 * never be one: the crawler owns the jobs table, and a dashboard that could
 * edit `notified` or `status` could silently un-send or re-send a digest.
 *
 * Lazy on purpose — nothing here runs until a row is actually expanded, so the
 * cost of both pages at rest is one company query and one count query.
 */
export async function GET(request: Request, ctx: { params: Promise<{ id: string }> }) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  const { id: rawId } = await ctx.params;
  const id = Number(rawId);
  if (!Number.isInteger(id) || id <= 0) return badRequest(`"${rawId}" is not a company id`);

  const filter = new URL(request.url).searchParams.get("filter") ?? "matches";
  if (filter !== "matches" && filter !== "all") {
    return badRequest(`filter must be "matches" or "all", not "${filter}"`);
  }

  try {
    if (filter === "all") {
      return NextResponse.json({ filter, jobs: await openJobsForCompany(id) });
    }
    const prefs = await loadPreferences();
    return NextResponse.json({ filter, jobs: await matchingJobsForCompany(id, prefs) });
  } catch (err) {
    return serverError(err, `GET /api/companies/${id}/jobs`);
  }
}
