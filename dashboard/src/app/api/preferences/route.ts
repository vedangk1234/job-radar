import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import {
  CITY_LABELS,
  CITY_UMBRELLAS,
  SALARY_BAND_VALUES,
  SELECTABLE_CITY_LABELS,
  WORK_MODE_VALUES,
} from "@/lib/labels";
import { NON_INDIA_LABEL } from "@/lib/match";
import { cleanList, KEYWORD_CAP, loadPreferences, savePreferences } from "@/lib/preferences";
import { watchesAnything } from "@/lib/watchlist";

/**
 * Tick-one-get-all, applied at the ROUTE and not only in the browser.
 *
 * The editor already sends members rather than umbrella names, so in practice
 * this is idempotent — which is the point. The editor is a convenience and this
 * is the rule, exactly as the keyword cap is enforced here rather than only on
 * the screen: an expansion that lives only in the browser is a suggestion to
 * anyone holding curl, and the row it would let them write is one the screen
 * cannot render back.
 *
 * SAVE-TIME EXPANSION IS THE WHOLE DESIGN DECISION OF THIS SLICE. selected_cities
 * stores MEMBERS, so notify/match.py never learns that "Mumbai region" is a thing
 * and the city gate stays vocabulary-blind in both languages — the property that
 * let the label set grow from 5 to 26 without either matcher changing. The cost is
 * stated rather than hidden: a city added to a group later does NOT reach accounts
 * that already saved, because their four members were frozen at save time. The
 * editor re-derives the umbrella checkbox as "all members ticked", so such an
 * account renders as partially-ticked and one save away from current — visible,
 * which is the direction this project errs in.
 */
function expandUmbrellas(chosen: string[]): string[] {
  const out = new Set(chosen);
  for (const { label, members } of CITY_UMBRELLAS) {
    if (out.has(label.toLowerCase())) {
      out.delete(label.toLowerCase());
      for (const member of members) out.add(member);
    }
  }
  return [...out];
}

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
 *   - an empty city answer or keywords list matches no job at all, so the digest
 *     goes quiet and looks like "nothing was posted" rather than "you broke the
 *     filter". Since cities Slice B the city answer is two fields — a selection
 *     or the all-India switch — and "empty" means neither was given;
 *   - a city label outside SELECTABLE_CITY_LABELS can never equal jobs.city, so
 *     it is a typo that costs a week of empty digests before anyone notices;
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

  const exclusions = cleanList(body.exclusions, { max: 300 });

  // NOT cleanList's `max`, and the difference is the whole point. That parameter
  // TRUNCATES — it stops copying at the limit and returns a shorter list, which
  // here would mean silently discarding the keywords past the twenty-fifth and
  // reporting success. A filter quietly rewritten is precisely the failure this
  // project's guards exist to prevent, so the list is cleaned in full and the
  // request is REFUSED if what survives is over the cap. Nothing is dropped on
  // anybody's behalf.
  const keywords = cleanList(body.keywords, { max: 300 });

  /*
   * THE CITY ANSWER — cities Slice B. Two fields that are alternatives, and the
   * validation is the third and outermost of the three layers that keep the
   * fail-closed rule true (the editor does not offer the two excluded labels,
   * this refuses them, and notify.match.CITY_SELECTION_NEVER_MATCHES is the
   * property for a row edited by hand).
   *
   * A STRICT BOOLEAN FOR all_india, for the reason show_no_experience_jobs is
   * checked the same way below: a PUT is a full document, so inferring FALSE from
   * an absent field would silently turn an all-India account back into one with
   * whatever city list happened to be on the row.
   */
  if (typeof body.all_india !== "boolean") {
    return badRequest(
      "all_india must be true or false — it decides whether the city selection is " +
        "read at all",
    );
  }
  const allIndia = body.all_india;

  // Cleaned in full and refused if over the cap, never truncated: `max: 20` used
  // to sit here and was a silent truncation waiting for a vocabulary change, which
  // the 25-city rewrite duly delivered. The argument is the keyword cap's below.
  const selectedCities = expandUmbrellas(cleanList(body.selected_cities, { max: 300 }));

  /*
   * EXCLUSIVE, AND REFUSED RATHER THAN RESOLVED. A body carrying both is a client
   * that disagrees with the server about what it just saved, and silently dropping
   * half of it is how a filter gets quietly rewritten — the failure this file's
   * validation exists to catch rather than commit. The matcher would ignore the
   * selection anyway (all_india branches first), which is exactly why the
   * disagreement has to surface here instead of becoming invisible.
   */
  if (allIndia && selectedCities.length) {
    return badRequest(
      `all_india is on and ${selectedCities.length} cit(y/ies) are also selected. ` +
        `All over India ignores the city list entirely — send one or the other, ` +
        `nothing has been changed.`,
    );
  }

  if (!allIndia) {
    if (!selectedCities.length) {
      return badRequest(
        "pick at least one city, or turn on All over India — an empty city " +
          "selection matches no job at all",
      );
    }
    if (selectedCities.length > SELECTABLE_CITY_LABELS.length) {
      return badRequest(
        `${selectedCities.length} cities is more labels than exist. There are ` +
          `${SELECTABLE_CITY_LABELS.length}; nothing has been changed.`,
      );
    }
    const unknown = selectedCities.filter((c) => !SELECTABLE_CITY_LABELS.includes(c));
    if (unknown.length) {
      // Two different mistakes, and they deserve different sentences. A label that
      // exists but is not selectable is a deliberate product rule somebody should
      // be told the reason for; anything else is a typo, which matches nothing
      // silently forever and is why this check exists at all.
      const notSelectable = unknown.filter((c) =>
        CITY_LABELS.includes(c as (typeof CITY_LABELS)[number]),
      );
      if (notSelectable.length) {
        return badRequest(
          `${notSelectable.join(", ")} cannot be chosen as a city. ` +
            `'india-unclear' means we could not tell where the job is, so it is ` +
            `covered by All over India rather than by picking a city; ` +
            `'${NON_INDIA_LABEL}' is outside this tool entirely.`,
        );
      }
      return badRequest(
        `unknown city label(s): ${unknown.join(", ")}. ` +
          `Choose from: ${SELECTABLE_CITY_LABELS.join(", ")}`,
      );
    }
  }

  /*
   * THE LEGACY COLUMN, DERIVED RATHER THAN ACCEPTED. `preferences.cities` is what
   * the DEPLOYED digest still reads, so this slice keeps writing it — see
   * lib/preferences.savePreferences for the expand-then-contract argument. It is
   * derived from the answer above rather than taken from the body, because a body
   * field nothing reads is a field that drifts from the one that decides.
   *
   * For an all-India account it is the 25 India labels: a rollback onto the old
   * matcher then gives that person the closest honest approximation of "anywhere
   * in India", rather than the empty list that would silently stop their digest.
   */
  const legacyCities = allIndia
    ? CITY_LABELS.filter((c) => c !== NON_INDIA_LABEL)
    : selectedCities;

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
   * THE WORK ARRANGEMENT — REQUIRED, and it belongs in the same family as the
   * empty-cities, empty-keywords and unset-range rules above rather than being a
   * new kind of check. lib/match.workModeOk treats an empty selection as
   * "matches nothing", so a row saved without one produces a digest that goes
   * quiet and reads as "nothing was posted".
   *
   * This refusal is the third of the three layers, and the only one that holds
   * for a row edited by hand: the picker offers exactly these three, this rejects
   * anything else, and notify.match.work_mode_ok is a plain membership test that
   * an unrecognised value simply fails.
   *
   * IT IS ALSO WHAT MAKES "ONBOARDED IMPLIES CONFIGURED" TRUE for this column,
   * the same construction the experience range relies on: this route is the only
   * writer of work_modes AND of onboarding_completed.
   */
  const workModes = cleanList(body.work_modes, { max: 10 });
  if (!workModes.length) {
    return badRequest(
      "pick at least one work arrangement — an empty selection matches no job at all",
    );
  }
  const unknownModes = workModes.filter((m) => !WORK_MODE_VALUES.includes(m));
  if (unknownModes.length) {
    return badRequest(
      `unknown work arrangement(s): ${unknownModes.join(", ")}. ` +
        `Choose from: ${WORK_MODE_VALUES.join(", ")}`,
    );
  }

  /*
   * THE PAY BANDS — AND THIS IS THE ONE FIELD IN THAT FAMILY THAT MAY BE EMPTY.
   *
   * Every check above refuses a blank because a blank matches no job at all and
   * the digest goes quiet in a way that reads as "nothing was posted". That
   * argument does not transfer: lib/match.salaryOk fails OPEN on an empty
   * selection, so a row saved without bands filters nothing and every job the
   * account would otherwise see still reaches it. Refusing here would be this
   * route inventing a requirement the matcher does not have — the mistake
   * notify/digest.assert_self_send was narrowed to stop making, one rule over.
   *
   * SO "ONBOARDED IMPLIES CONFIGURED" DOES NOT EXTEND TO salary_bands, and that
   * is worth stating because it holds for selected_cities, work_modes and the
   * experience range BY CONSTRUCTION — this route is their only writer and it is
   * also the only writer of onboarding_completed. An onboarded account can have
   * an empty salary_bands forever, deliberately, exactly as it can have empty
   * exclusions and a null years_of_experience_held.
   *
   * The VALUES are still checked. A typo cannot match a band bound, so it would
   * narrow the filter to nothing-recognised and hide every priced job silently —
   * the same reason an unknown city label is refused rather than ignored.
   */
  const salaryBands = cleanList(body.salary_bands, { max: 20 });
  const unknownBands = salaryBands.filter((b) => !SALARY_BAND_VALUES.includes(b));
  if (unknownBands.length) {
    return badRequest(
      `unknown salary band(s): ${unknownBands.join(", ")}. ` +
        `Choose from: ${SALARY_BAND_VALUES.join(", ")}`,
    );
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

  /*
   * THE WATCH LIST — REQUIRED, AND IT IS THE FIFTH MEMBER OF THE FAMILY ABOVE
   * RATHER THAN A NEW KIND OF RULE.
   *
   * Every other refusal in this route guards the same failure: a row that matches
   * no job at all, producing a digest that goes quiet and reads as "nothing was
   * posted". An empty watch list is that failure in its purest form, and it does
   * not even need a matcher to produce it — notify/store.CANDIDATES_SQL asks
   * `EXISTS (user_companies …)` FIRST, so an account watching nothing has an empty
   * queue however good its keywords are.
   *
   * IT IS ALSO WHAT THIS SLICE EXISTS TO FIX. Until the picker there was no writer
   * for that table in the web app at all, so EVERY account that completed
   * onboarding was in exactly this state — filter chosen, inbox permanently empty,
   * and a Home screen showing the whole shared catalog as though those companies
   * were theirs. Letting a save through without one would leave the hole open on
   * the commit that built the control for closing it.
   *
   * WHY REFUSE RATHER THAN DEFAULT TO SOMETHING. The obvious alternative is to
   * auto-watch the catalog (or its top N) for a new account, and it is the
   * archive-dump failure with a friendly face: fifty companies' stored history
   * suppressed on somebody's behalf, a digest whose contents nobody chose, and the
   * "seeded defaults" mistake Slices 1b and 1c spent two commits removing from the
   * preferences row. Fail-closed is the same answer the city selection gets, for
   * the same reason.
   *
   * CHECKED LAST, after every field-level validation, so a body that is also
   * malformed is told about the malformation first — this is the one refusal here
   * that is about the STATE of the account rather than about the request, and
   * reporting it before a typo'd city label would send somebody to the wrong
   * screen.
   */
  if (!(await watchesAnything(gate.session.userId))) {
    return badRequest(
      "you are not watching any company, so this filter would match nothing at all. " +
        "Pick at least one on the Watchlist tab and save again — your digest is built " +
        "from the companies you watch first, and the filter narrows those.",
    );
  }

  try {
    const preferences = await savePreferences(gate.session.userId, {
      cities: [...legacyCities],
      selected_cities: selectedCities,
      all_india: allIndia,
      keywords,
      exclusions,
      max_level: maxLevel,
      exp_range_low: expLow,
      exp_range_high: expHigh,
      years_of_experience_held: yearsHeld,
      show_no_experience_jobs: showNoExperienceJobs,
      work_modes: workModes,
      salary_bands: salaryBands,
    });
    return NextResponse.json({
      preferences,
      message:
        `Saved. ${keywords.length} keyword(s), ` +
        `${allIndia ? "all of India" : `${selectedCities.length} cit(y/ies)`}, ` +
        `${exclusions.length} exclusion(s), max_level ${maxLevel}, ` +
        `${workModes.join("/")}, ` +
        `${salaryBands.length ? `${salaryBands.length} pay band(s)` : "any pay"}, ` +
        `looking for ${expLow}-${expHigh} years` +
        `${showNoExperienceJobs ? ", including" : ", excluding"} jobs that state no minimum. ` +
        `The next digest uses this.`,
    });
  } catch (err) {
    return serverError(err, "PUT /api/preferences");
  }
}
