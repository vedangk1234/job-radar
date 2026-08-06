import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import {
  CITY_LABELS,
  cleanList,
  KEYWORD_CAP,
  loadPreferences,
  savePreferences,
} from "@/lib/preferences";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;
  try {
    return NextResponse.json({ preferences: await loadPreferences(gate.session.userId) });
  } catch (err) {
    return serverError(err, "GET /api/preferences");
  }
}

/**
 * Replace the preferences row wholesale.
 *
 * PUT rather than PATCH because the editor sends the full lists it is showing:
 * over one row owned by one user, last-write-wins on a complete document is
 * simpler to reason about than merging partial updates, and there is no second
 * writer to lose an edit to. The row is resolved by owner in lib/preferences.ts,
 * so this route names no id — the owner arrives with the session instead.
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
 *
 * SINCE SLICE 1 THIS IS ALSO THE KEYWORD CAP, and it is here rather than only in
 * the editor because the editor is a convenience and this is the rule. It is
 * also the only writer of onboarding_completed — see lib/preferences.savePreferences.
 * There is deliberately no second route for saving keywords: the resume upload
 * SUGGESTS them and this is what stores them, so there is exactly one statement
 * in the app that can write preferences.keywords and exactly one place the cap
 * can be enforced.
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
  const exclusions = cleanList(body.exclusions, { max: 300 });

  // NOT cleanList's `max`, and the difference is the whole point. That parameter
  // TRUNCATES — it stops copying at the limit and returns a shorter list, which
  // here would mean silently discarding the keywords past the twenty-fifth and
  // reporting success. A filter quietly rewritten is precisely the failure this
  // project's guards exist to prevent, so the list is cleaned in full and the
  // request is REFUSED if what survives is over the cap. Nothing is dropped on
  // anybody's behalf.
  const keywords = cleanList(body.keywords, { max: 300 });

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
  if (keywords.length > KEYWORD_CAP) {
    return badRequest(
      `${keywords.length} keywords is over the limit of ${KEYWORD_CAP}. Remove ` +
        `${keywords.length - KEYWORD_CAP} and save again — nothing has been changed, ` +
        `and your current filter is still running.`,
    );
  }

  const maxLevel = Number(body.max_level);
  if (!Number.isInteger(maxLevel) || maxLevel < 0 || maxLevel > 5) {
    return badRequest("max_level must be an integer from 0 to 5");
  }

  try {
    const preferences = await savePreferences(gate.session.userId, {
      cities,
      keywords,
      exclusions,
      max_level: maxLevel,
    });
    return NextResponse.json({
      preferences,
      message: `Saved. ${keywords.length} keyword(s), ${cities.length} cit(y/ies), ${exclusions.length} exclusion(s), max_level ${maxLevel}. The next digest uses this.`,
    });
  } catch (err) {
    return serverError(err, "PUT /api/preferences");
  }
}
