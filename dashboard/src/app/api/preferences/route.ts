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
 *     typo that costs a week of empty digests before anyone notices;
 *   - an incomplete EXPERIENCE RANGE matches no job at all either, since Phase 3
 *     Slice 2 — see the block below, which is the third member of exactly this
 *     family rather than a new kind of rule.
 *
 * Exclusions may be empty — that is a real, meaningful choice. So is
 * years_of_experience_held, which is informational and never filters.
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

  // `max: 20` here was a silent truncation waiting for a vocabulary change, and
  // the 25-city rewrite is that change: CITY_LABELS is 26 entries, so an account
  // ticking every box would have had six of them dropped and been told the save
  // succeeded. Cleaned in full and refused if over the cap, exactly like the
  // keywords below — the argument in that comment is this one, and it applies
  // here for the same reason. The cap is above the vocabulary rather than equal
  // to it so adding a city is not also a schema-shaped edit to this file.
  const cities = cleanList(body.cities, { max: 300 });
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
  if (cities.length > CITY_LABELS.length) {
    return badRequest(
      `${cities.length} cities is more labels than exist. There are ` +
        `${CITY_LABELS.length}; nothing has been changed.`,
    );
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

  /*
   * THE EXPERIENCE RANGE — REQUIRED, and it belongs in the same paragraph of the
   * doc comment above as the empty-cities and empty-keywords rules, because it is
   * the same failure. lib/match.experienceExcluded treats an unconfigured range as
   * "matches nothing", so a row saved without one would produce a digest that goes
   * quiet and reads as "nothing was posted" rather than "the filter is unset".
   *
   * This refusal is also what makes "onboarded implies configured" true: this
   * route is the only writer of these columns AND of onboarding_completed, so a
   * save that gets past here is the only way an account becomes onboarded.
   *
   * EXPLICIT null / undefined CHECKS BEFORE Number(), because Number(null) is 0
   * and Number(undefined) is NaN. Without them a missing exp_range_low would be
   * accepted as a perfectly valid 0 — a filter silently widened to include
   * everything from zero years, which is the shape of failure this file's
   * validation exists to catch rather than commit.
   */
  const RANGE_CEILING = 20;
  if (body.exp_range_low === null || body.exp_range_low === undefined ||
      body.exp_range_high === null || body.exp_range_high === undefined) {
    return badRequest(
      "an experience range is required — an unset range matches no job at all. " +
        "Give exp_range_low and exp_range_high as whole years.",
    );
  }
  const expLow = Number(body.exp_range_low);
  const expHigh = Number(body.exp_range_high);
  if (!Number.isInteger(expLow) || expLow < 0 || expLow > RANGE_CEILING) {
    return badRequest(`exp_range_low must be a whole number of years from 0 to ${RANGE_CEILING}`);
  }
  if (!Number.isInteger(expHigh) || expHigh < 0 || expHigh > RANGE_CEILING) {
    return badRequest(`exp_range_high must be a whole number of years from 0 to ${RANGE_CEILING}`);
  }
  if (expLow > expHigh) {
    return badRequest(
      `the experience range is inverted: ${expLow} to ${expHigh}. The low end must not ` +
        `exceed the high end (they may be equal, which looks for exactly that year).`,
    );
  }

  /*
   * A STRICT BOOLEAN RATHER THAN A TRUTHINESS TEST. A PUT is a full document, so
   * treating an absent field as FALSE would be defensible in general — and here it
   * would hide every job whose description states no minimum, which on the live
   * database is 99% of the open board. A filter that empties itself because a
   * field was omitted is not a thing to infer, so it is refused instead.
   */
  if (typeof body.show_no_experience_jobs !== "boolean") {
    return badRequest(
      "show_no_experience_jobs must be true or false — it decides what happens to " +
        "jobs whose description states no minimum, which is most of them",
    );
  }
  const showNoExperienceJobs = body.show_no_experience_jobs;

  /*
   * OPTIONAL, AND NEVER CHECKED AGAINST THE RANGE. This is how much experience the
   * person HAS; the range is what they are LOOKING FOR, and the two are
   * independent — holding five years and looking at 0-2 roles is a legitimate
   * answer, not a contradiction to validate away. It filters nothing (see
   * lib/preferences.savePreferences), so null is a permanent legitimate value.
   */
  const heldRaw = body.years_of_experience_held;
  let yearsHeld: number | null = null;
  if (heldRaw !== null && heldRaw !== undefined && heldRaw !== "") {
    yearsHeld = Number(heldRaw);
    if (!Number.isInteger(yearsHeld) || yearsHeld < 0 || yearsHeld > 50) {
      return badRequest("years_of_experience_held must be null or a whole number from 0 to 50");
    }
  }

  try {
    const preferences = await savePreferences(gate.session.userId, {
      cities,
      keywords,
      exclusions,
      max_level: maxLevel,
      exp_range_low: expLow,
      exp_range_high: expHigh,
      years_of_experience_held: yearsHeld,
      show_no_experience_jobs: showNoExperienceJobs,
    });
    return NextResponse.json({
      preferences,
      message:
        `Saved. ${keywords.length} keyword(s), ${cities.length} cit(y/ies), ` +
        `${exclusions.length} exclusion(s), max_level ${maxLevel}, ` +
        `looking for ${expLow}-${expHigh} years` +
        `${showNoExperienceJobs ? ", including" : ", excluding"} jobs that state no minimum. ` +
        `The next digest uses this.`,
    });
  } catch (err) {
    return serverError(err, "PUT /api/preferences");
  }
}
