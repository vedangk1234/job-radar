import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { CITY_LABELS, cleanList, loadPreferences, savePreferences } from "@/lib/preferences";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;
  try {
    return NextResponse.json({ preferences: await loadPreferences() });
  } catch (err) {
    return serverError(err, "GET /api/preferences");
  }
}

/**
 * Replace the preferences row wholesale.
 *
 * PUT rather than PATCH because the editor sends the full lists it is showing:
 * with a single shared row and one user, last-write-wins over a complete
 * document is simpler to reason about than merging partial updates, and there
 * is no second writer to lose an edit to.
 *
 * Validation is strict about two things, both because the failure is SILENT:
 *
 *   - an empty cities or keywords list matches no job at all, so the digest
 *     goes quiet and looks like "nothing was posted" rather than "you broke the
 *     filter";
 *   - a city label outside CITY_LABELS can never equal jobs.city, so it is a
 *     typo that costs a week of empty digests before anyone notices.
 *
 * Exclusions may be empty — that is a real, meaningful choice.
 */
export async function PUT(request: Request) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return badRequest("body was not JSON");
  }

  const cities = cleanList(body.cities, { max: 20 });
  const keywords = cleanList(body.keywords, { max: 300 });
  const exclusions = cleanList(body.exclusions, { max: 300 });

  if (!cities.length) {
    return badRequest("cities cannot be empty — an empty city list matches no job at all");
  }
  const unknown = cities.filter((c) => !CITY_LABELS.includes(c as (typeof CITY_LABELS)[number]));
  if (unknown.length) {
    return badRequest(
      `unknown city label(s): ${unknown.join(", ")}. ` +
        `city_label only ever produces: ${CITY_LABELS.join(", ")}`,
    );
  }
  if (!keywords.length) {
    return badRequest("keywords cannot be empty — a title must match at least one to be sent");
  }

  const maxLevel = Number(body.max_level);
  if (!Number.isInteger(maxLevel) || maxLevel < 0 || maxLevel > 5) {
    return badRequest("max_level must be an integer from 0 to 5");
  }

  try {
    const preferences = await savePreferences({ cities, keywords, exclusions, max_level: maxLevel });
    return NextResponse.json({
      preferences,
      message: `Saved. ${keywords.length} keyword(s), ${cities.length} cit(y/ies), ${exclusions.length} exclusion(s), max_level ${maxLevel}. The next digest uses this.`,
    });
  } catch (err) {
    return serverError(err, "PUT /api/preferences");
  }
}
