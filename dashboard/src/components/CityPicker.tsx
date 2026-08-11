"use client";

import { CITY_LABEL_GROUPS, cityDisplayName } from "@/lib/labels";

/**
 * The city half of the preferences screen — Phase 3, cities Slice B.
 *
 * Its own component rather than another block inside PreferencesEditor, which was
 * already the longest file in the app before this slice added an umbrella rule and
 * an exclusive switch to it. The editor still owns the STATE (one Save writes one
 * coherent filter, which is the whole shape of that screen); this owns the
 * controls and the one derived rule that is easy to get subtly wrong.
 *
 * THE THREE THINGS ON SCREEN, and what each writes:
 *
 *   All over India   ->  all_india, and nothing else. While it is on the grid is
 *                        disabled rather than hidden, so a person can see what
 *                        their selection WAS and what turning it off returns them
 *                        to. Hiding it would make the switch feel like a mode
 *                        change and lose the answer they already gave.
 *   an umbrella      ->  every member of its group. It is not stored: see
 *                        api/preferences.expandUmbrellas for why expansion happens
 *                        at save time and what that costs.
 *   a city           ->  itself.
 *
 * WHAT IS NOT ON SCREEN, and this is the fail-closed half of the slice.
 * `india-unclear` and `non-india` are absent from CITY_LABEL_GROUPS, so there is
 * no control for them here at all. india-unclear means "we could not tell where
 * this job is" — a statement about our confidence, not about a location — so it
 * belongs to the All over India switch, which is what the hint says in those
 * words. Offering a checkbox that the route refuses and the matcher can never
 * match is the silent-empty failure CITY_LABELS exists to prevent, so it is not
 * offered.
 */
export function CityPicker({
  selected,
  onSelectedChange,
  allIndia,
  onAllIndiaChange,
}: {
  selected: string[];
  onSelectedChange: (next: string[]) => void;
  allIndia: boolean;
  onAllIndiaChange: (next: boolean) => void;
}) {
  function toggleCity(label: string, on: boolean) {
    onSelectedChange(on ? [...selected, label] : selected.filter((c) => c !== label));
  }

  /*
   * Tick the umbrella -> every member; untick -> every member removed, and NOTHING
   * outside the group is touched either way. That last clause is the one worth
   * stating: the two umbrellas are disjoint today, and writing the removal as a
   * set difference rather than as "keep only what is outside this group" is what
   * keeps it true if a label ever appears in two groups.
   */
  function toggleUmbrella(members: readonly string[], on: boolean) {
    const rest = selected.filter((c) => !members.includes(c));
    onSelectedChange(on ? [...rest, ...members] : rest);
  }

  return (
    <div className="panel">
      {/*
        THE THREE WIDE ANSWERS, TOGETHER AND FIRST — the design's pill row. All
        over India is the exclusive switch; the two umbrellas are tick-one-get-all
        over their own group and nothing else.

        THE UMBRELLAS CARRY THEIR OWN `disabled` NOW, and that is load-bearing
        rather than tidy. They used to sit inside the fieldset below, which
        disabled them for free while All over India was on; lifting them out of it
        would otherwise leave two live controls writing a city selection the
        all-India answer ignores — two answers on screen at once, which is exactly
        what PUT /api/preferences refuses a body for stating.
      */}
      <div className="city-umbrellas pills">
        <label className="switch">
          <input
            type="checkbox"
            checked={allIndia}
            onChange={(e) => onAllIndiaChange(e.target.checked)}
            aria-label="All over India"
          />
          <span>All over India</span>
        </label>

        {CITY_LABEL_GROUPS.filter((group) => group.umbrella).map((group) => {
          const allOn = group.labels.every((l) => selected.includes(l));
          const someOn = group.labels.some((l) => selected.includes(l));
          return (
            <label className="switch" key={group.umbrella}>
              <input
                type="checkbox"
                checked={allOn}
                disabled={allIndia}
                // Indeterminate is a DOM property, not an attribute, so React
                // cannot set it from JSX — hence the ref. It is what shows a
                // half-ticked group honestly rather than as "off", which is
                // the state an account lands in when a city is added to a
                // group after they last saved. See expandUmbrellas.
                ref={(el) => {
                  if (el) el.indeterminate = !allOn && someOn;
                }}
                onChange={(e) => toggleUmbrella(group.labels, e.target.checked)}
                aria-label={group.umbrella}
              />
              <span>{group.umbrella}</span>
            </label>
          );
        })}
      </div>

      <p className="hint">
        <strong>All over India</strong> matches every Indian posting, including the ones whose
        location we could not pin to a city (&ldquo;Remote&rdquo;, bare &ldquo;India&rdquo;, a
        state name) and the ones in towns outside the list below. Jobs outside India are never
        sent either way. While it is on, the city list is ignored entirely &mdash; it is one
        answer or the other, not both.
      </p>

      <fieldset
        disabled={allIndia}
        style={{ border: 0, padding: 0, margin: 0, opacity: allIndia ? 0.45 : 1 }}
      >
        <p className="hint">
          Or pick the cities you want. A posting is labelled with the city it names, and
          <strong> only a posting labelled with one of your cities is sent</strong> &mdash; a
          job whose location we could not read does not slip through. Choose{" "}
          <strong>Elsewhere in India</strong> for named Indian places outside this list
          (Lucknow, Mohali, Patna).
        </p>

        {/*
          THE NAME IS FOR THE READER, THE LABEL IS THE ANSWER. Every cell prints
          `cityDisplayName(label)` and carries the slug on `title` and in the
          accessible name — `blr` is what jobs.city holds and what
          selected_cities stores, and nothing about that changed here. See
          lib/labels.CITY_DISPLAY_NAMES.
        */}
        {CITY_LABEL_GROUPS.map((group) => (
          <div key={group.heading} className="city-group">
            <p className="city-group-heading">{group.heading}</p>
            <div className="city-grid">
              {group.labels.map((label) => (
                <label key={label} className="switch" title={label}>
                  <input
                    type="checkbox"
                    checked={selected.includes(label)}
                    onChange={(e) => toggleCity(label, e.target.checked)}
                    aria-label={`${cityDisplayName(label)} (${label})`}
                  />
                  <span className="city-name">{cityDisplayName(label)}</span>
                </label>
              ))}
            </div>
          </div>
        ))}
      </fieldset>

      {!allIndia && !selected.length ? (
        <p className="msg bad">
          An empty city list matches nothing. Pick at least one, or turn on All over India.
        </p>
      ) : null}
    </div>
  );
}
