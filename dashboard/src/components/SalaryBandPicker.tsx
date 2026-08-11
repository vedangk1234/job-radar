"use client";

import { SALARY_BAND_OPTIONS } from "@/lib/labels";

/**
 * The pay-band half of the preferences screen — the salary slice.
 *
 * Its own component for the reason CityPicker and WorkModePicker are:
 * PreferencesEditor owns the STATE, because one Save has to write one coherent
 * filter, and the controls live beside the rule they express.
 *
 * FLAT MULTI-SELECT, NO UMBRELLA AND NO SWITCH — the WorkModePicker argument, and
 * one extra reason specific to this panel. An "any pay" switch would be a second
 * way to say what ticking nothing already says, and here that is not merely
 * redundant but actively misleading: ticking nothing and ticking all six produce
 * IDENTICAL results, so a switch would imply a distinction the matcher does not
 * make.
 *
 * THE EMPTY STATE IS NOT AN ERROR HERE, AND THAT IS THE DIFFERENCE FROM EVERY
 * OTHER PANEL ON THIS SCREEN. WorkModePicker shows a red "no job can match at
 * all" when nothing is ticked, and the city and experience panels block Save
 * outright. This one says the opposite, because lib/match.salaryOk fails OPEN:
 * an empty selection filters nothing. A person who unticks everything here has
 * turned the filter off, not broken it.
 *
 * THE HINT HAS TO SAY THE QUIET PART. Unlike every other filter on this screen,
 * this one changes almost nothing: measured against the live board, zero of the
 * 36 sampled postings stated a number, and a job with no stated pay is shown
 * regardless of what is ticked. So somebody who ticks "Under ₹10L" expecting a
 * short list gets the entire board and reasonably concludes the feature is
 * broken. Saying so on the screen is the same courtesy the on-site hint and the
 * "99% of postings state no minimum" hint already pay, and it is more necessary
 * here than in either of those places.
 */
export function SalaryBandPicker({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  function toggle(value: string, on: boolean) {
    onChange(on ? [...selected, value] : selected.filter((b) => b !== value));
  }

  return (
    <div className="panel">
      <h2>
        Expected pay{" "}
        <span className="muted small">
          ({selected.length} of {SALARY_BAND_OPTIONS.length})
        </span>
      </h2>
      <p className="hint">
        Optional. Most job postings do not state a salary at all &mdash; those always reach you,
        whatever you pick here. Ticking bands only narrows the few postings that DO state their
        pay, so leaving this blank and ticking everything do the same thing.
      </p>

      <div className="chips" style={{ marginTop: 12 }}>
        {SALARY_BAND_OPTIONS.map((option) => (
          <label key={option.value} className="switch" style={{ marginRight: 16 }}>
            <input
              type="checkbox"
              checked={selected.includes(option.value)}
              onChange={(e) => toggle(option.value, e.target.checked)}
              aria-label={option.label}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>

      {selected.length === 0 ? (
        <p className="hint" style={{ marginTop: 12 }}>
          Nothing ticked, so pay is not filtered on. Every job reaches you regardless of what it
          says it pays.
        </p>
      ) : null}
    </div>
  );
}
