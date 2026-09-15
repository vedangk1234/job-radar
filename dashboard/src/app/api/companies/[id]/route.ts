import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import {
  companyCard,
  NoSuchCompanyError,
  setTracked,
  TrackedCapError,
  updateCompany,
} from "@/lib/companies";
import { CATEGORIES } from "@/lib/labels";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * PATCH { category?: string, tracked?: boolean } — the two fields the dashboard
 * may change on a company.
 *
 * The response carries the whole re-read card, counts included, so the client
 * re-renders from what the database now says rather than from what it guessed.
 * That matters more than it looks: the optimistic control in the UI moves before
 * the round trip, and this response is what corrects it if the write did
 * something other than what the click assumed.
 *
 * `tracked` IS ACCEPTED AGAIN — the crawl-selection slice. It is the crawler's
 * catalog-wide flag (scripts/crawl.py reads `WHERE tracked = TRUE`), one shared
 * value for the whole install, and it is editable by any signed-in user on a
 * deliberate, documented decision: the operator wants to curate the crawled set
 * from the dashboard, and the app has one real user. It is capped — lib/companies
 * .setTracked enforces TRACKED_CAP inside a transaction — so turning a company on
 * past the limit is refused with 409 and nothing is written.
 *
 * This is still NOT the per-user watch list. `user_companies` ("I want alerts for
 * this company") is a different table with its own route (POST/DELETE
 * /api/watchlist). This route edits the SHARED catalog row; that one edits a
 * statement about one person. category and tracked are handled as separate
 * branches so a body may carry either.
 */
export async function PATCH(request: Request, ctx: { params: Promise<{ id: string }> }) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  const { id: rawId } = await ctx.params;
  const id = Number(rawId);
  if (!Number.isInteger(id) || id <= 0) return badRequest(`"${rawId}" is not a company id`);

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return badRequest("body was not JSON");
  }

  const hasCategory = "category" in body;
  const hasTracked = "tracked" in body;
  if (!hasCategory && !hasTracked) {
    return badRequest("nothing to update: send category and/or tracked");
  }

  if (hasTracked && typeof body.tracked !== "boolean") {
    return badRequest("tracked must be true or false");
  }

  let category: string | undefined;
  if (hasCategory) {
    category = String(body.category ?? "").trim().toLowerCase();
    // A closed list, checked here rather than by a CHECK constraint: the column
    // is documented prose in db/schema.sql, and a typo'd tag is a company that
    // quietly stops matching any grouping anyone later writes.
    if (!(CATEGORIES as readonly string[]).includes(category)) {
      return badRequest(`category must be one of ${CATEGORIES.join(", ")}`);
    }
  }

  try {
    if (hasCategory) {
      const found = await updateCompany(id, { category });
      if (!found) return NextResponse.json({ error: "not_found" }, { status: 404 });
    }
    if (hasTracked) {
      await setTracked(id, body.tracked as boolean);
    }
    return NextResponse.json({ company: await companyCard(id, gate.session.userId) });
  } catch (err) {
    if (err instanceof NoSuchCompanyError) {
      return NextResponse.json({ error: "not_found" }, { status: 404 });
    }
    // 409 Conflict: the request was valid but the install is at its crawl limit.
    // A human-readable message so the picker can show it verbatim.
    if (err instanceof TrackedCapError) {
      return NextResponse.json({ error: "tracked_cap", detail: err.message }, { status: 409 });
    }
    return serverError(err, `PATCH /api/companies/${id}`);
  }
}
