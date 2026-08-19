import { CompanyLogo } from "@/components/CompanyLogo";
import { freshnessLabel, topCityChips } from "@/lib/cards";
import type { CompanyCard } from "@/lib/companies";

/**
 * THE FACE OF A CARD — everything visible before it is opened.
 *
 * Its own file rather than JSX inside CompanyList for the reason JobLine has one:
 * it is the piece whose OUTPUT is worth checking. A chip row that quietly stops
 * rendering, a big number that reads the wrong field, a freshness tag on a
 * company with nothing relevant — none of those is something a type checker or a
 * query can catch, and `npm run ui-check` renders this component over live cards
 * and asserts what the markup actually contains.
 *
 * WHAT IT IS NOT: interactive. Every control on a card — the disclosure, the
 * category select, the Applied boxes — lives outside or below this, in
 * CompanyList and the panel. This is a label, so it can be rendered anywhere,
 * including inside the <button> that opens the card. That last part is a
 * constraint rather than a convenience: nesting a <select> inside a <button> is
 * invalid HTML and behaves accordingly, which is exactly why the category control
 * moved to the panel when the row became a card (see CompanyList).
 *
 * THE ONE NUMBER WITH A COLOUR is the relevant count, and only when it is not
 * zero. Fifty cards each shouting a figure is fifty figures nobody reads; the
 * filler cards state their zero quietly and the eye lands on the companies that
 * have something.
 */
export function CompanyCardHead({ company, now }: { company: CompanyCard; now?: Date }) {
  const relevant = company.match_jobs;
  const { chips, more } = topCityChips(company.city_counts);
  // `now` is threaded rather than read here so the whole card is a pure function
  // of its props — see lib/cards.freshnessLabel for why the clock is an argument.
  const fresh = freshnessLabel(company.latest_match_at, now);

  return (
    <span className="card-face">
      <span className="card-top">
        <CompanyLogo name={company.name} />
        <span className="card-ident">
          <span className="card-name">{company.name}</span>
          {fresh ? (
            <span
              className="card-fresh"
              title={
                company.latest_match_at
                  ? `The freshest role that fits you here was first seen at ${company.latest_match_at}.`
                  : undefined
              }
            >
              {fresh}
            </span>
          ) : null}
        </span>
      </span>

      <span className={`card-count${relevant ? " hit" : ""}`}>
        <strong className="num">{relevant}</strong>
        <span className="card-count-word">
          relevant role{relevant === 1 ? "" : "s"}
        </span>
      </span>

      {/*
        THE CHIP ROW, AND EVERY NUMBER IN IT IS INSIDE YOUR CITY ANSWER. A role in
        a city you did not pick is not a match, so it is not in these counts and
        not on this row — see lib/jobs.matchCountsByCompany. The big number above
        can exceed the three chips shown, which is what `+more` is for.

        Rendered only when there is something to say. A company with no relevant
        role gets no chip row rather than a row of zeroes.
      */}
      {relevant > 0 ? (
        <span className="card-chips">
          <span className="chip-total">
            {relevant} role{relevant === 1 ? "" : "s"}
          </span>
          {chips.map((chip) => (
            <span
              key={chip.label}
              className="chip-city"
              title={`${chip.count} in ${chip.name} (${chip.label.toLowerCase()})`}
            >
              {chip.count} {chip.label}
            </span>
          ))}
          {more > 0 ? (
            <span className="chip-more" title={`${more} more cit${more === 1 ? "y" : "ies"}`}>
              +more
            </span>
          ) : null}
        </span>
      ) : null}

      <span className="card-foot">
        {/*
          Faint, secondary, and the same fact the collapsible row printed as
          "total jobs": OPEN jobs, with the closed count in the tooltip. Kept
          identical rather than restated so the card and /jobs cannot drift into
          two definitions of "total".
        */}
        <span
          className="muted small"
          title={`${company.total_jobs} rows stored in all, ${
            company.total_jobs - company.open_jobs
          } of them closed`}
        >
          {company.open_jobs} total job{company.open_jobs === 1 ? "" : "s"}
        </span>
        {company.applied_jobs > 0 ? (
          <span
            className="muted small"
            title={`${company.applied_jobs} of the ${relevant} roles that fit you here are ticked as applied.`}
          >
            {company.applied_jobs} applied
          </span>
        ) : null}
      </span>
    </span>
  );
}
