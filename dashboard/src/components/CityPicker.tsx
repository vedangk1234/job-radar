"use client";

import { CITY_LABEL_GROUPS } from "@/lib/labels";

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
      <h2>Cities</h2>

      <label className="switch">
        <input
          type="checkbox"
          checked={allIndia}
          onChange={(e) => onAllIndiaChange(e.target.checked)}
          aria-label="All over India"
        />
        <span>
          <strong>All over India</strong> &mdash; anywhere in the country
        </span>
      </label>
      <p className="hint">
        Matches every Indian posting, including the ones whose location we could not pin to a
        city (&ldquo;Remote&rdquo;, bare &ldquo;India&rdquo;, a state name) and the ones in
        towns outside the list below. Jobs outside India are never sent either way. While this
        is on, the city list is ignored entirely &mdash; it is one answer or the other, not
        both.
      </p>

      <fieldset
        disabled={allIndia}
        style={{ border: 0, padding: 0, margin: 0, opacity: allIndia ? 0.45 : 1 }}
      >
        <p className="hint">
          Or pick the cities you want. A posting is labelled with the city it names, and
          <strong> only a posting labelled with one of your cities is sent</strong> &mdash; a
          job whose location we could not read does not slip through. Choose{" "}
          <code>other-india</code> for named Indian places outside this list (Lucknow, Mohali,
          Patna).
        </p>

        {CITY_LABEL_GROUPS.map((group) => {
          const allOn = group.labels.every((l) => selected.includes(l));
          const someOn = group.labels.some((l) => selected.includes(l));
          return (
            <div key={group.heading} style={{ marginBottom: 10 }}>
              <p className="hint" style={{ margin: "0 0 4px" }}>
                {group.heading}
              </p>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 14 }}>
                {group.umbrella ? (
                  <label className="switch">
                    <input
                      type="checkbox"
                      checked={allOn}
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
                    <span>
                      <strong>{group.umbrella}</strong>
                    </span>
                  </label>
                ) : null}
                {group.labels.map((label) => (
                  <label key={label} className="switch">
                    <input
                      type="checkbox"
                      checked={selected.includes(label)}
                      onChange={(e) => toggleCity(label, e.target.checked)}
                    />
                    <span className="mono">{label}</span>
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </fieldset>

      {!allIndia && !selected.length ? (
        <p className="msg bad">
          An empty city list matches nothing. Pick at least one, or turn on All over India.
        </p>
      ) : null}
    </div>
  );
}
