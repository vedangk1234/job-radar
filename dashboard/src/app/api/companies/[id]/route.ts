import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { companyCard, updateCompany } from "@/lib/companies";
import { CATEGORIES } from "@/lib/labels";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * PATCH { tracked?: boolean, category?: string } — the only two fields the
 * dashboard may change on a company, and the only two it has ever been able to.
 *
 * The response carries the whole re-read card, counts included, so the client
 * re-renders from what the database now says rather than from what it guessed.
 * That matters more than it looks: the optimistic toggle in the UI flips the
 * switch before the round trip, and this response is what corrects it if the
 * write did something other than what the click assumed.
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

  const patch: { tracked?: boolean; category?: string } = {};

  if ("tracked" in body) {
    if (typeof body.tracked !== "boolean") return badRequest("tracked must be true or false");
    patch.tracked = body.tracked;
  }

  if ("category" in body) {
    const category = String(body.category ?? "").trim().toLowerCase();
    // A closed list, checked here rather than by a CHECK constraint: the column
    // is documented prose in db/schema.sql, and a typo'd tag is a company that
    // quietly stops matching any grouping anyone later writes.
    if (!(CATEGORIES as readonly string[]).includes(category)) {
      return badRequest(`category must be one of ${CATEGORIES.join(", ")}`);
    }
    patch.category = category;
  }

  if (patch.tracked === undefined && patch.category === undefined) {
    return badRequest("nothing to update: send tracked and/or category");
  }

  try {
    const found = await updateCompany(id, patch);
    if (!found) return NextResponse.json({ error: "not_found" }, { status: 404 });
    return NextResponse.json({ company: await companyCard(id, gate.session.userId) });
  } catch (err) {
    return serverError(err, `PATCH /api/companies/${id}`);
  }
}
