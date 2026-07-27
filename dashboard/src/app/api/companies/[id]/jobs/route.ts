import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { matchingJobsForCompany, openJobsWithMatchFlag } from "@/lib/jobs";
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
 * Both filters load the preferences row, including `all`, which does not use it
 * to select anything. It uses it to LABEL: each row comes back with a `matches`
 * flag, which is the SUGGESTED badge on the raw list. Same shared filter as
 * Home, evaluated at request time — see lib/badges.ts for why that verdict is
 * derived per request instead of stored on the row.
 *
 * Read-only. There is no PATCH, POST or DELETE in this file and there should
 * never be one: the crawler owns the lifecycle of a job, and a route that could
 * edit `notified` or `status` could silently un-send or re-send a digest. The
 * one column the dashboard does own has its own route — PATCH
 * /api/jobs/applied — deliberately kept out of this file so that "the route
 * that lists jobs cannot change them" stays true by inspection.
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
    const prefs = await loadPreferences();
    const jobs =
      filter === "all"
        ? await openJobsWithMatchFlag(id, prefs)
        : await matchingJobsForCompany(id, prefs);
    return NextResponse.json({ filter, jobs });
  } catch (err) {
    return serverError(err, `GET /api/companies/${id}/jobs`);
  }
}
