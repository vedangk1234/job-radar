"use client";

import { WORK_MODE_OPTIONS } from "@/lib/labels";

/**
 * The work-arrangement half of the preferences screen — the work-mode slice.
 *
 * Its own component for the reason CityPicker is: PreferencesEditor owns the
 * STATE, because one Save has to write one coherent filter, and the controls live
 * beside the rule they express.
 *
 * FLAT MULTI-SELECT, NO UMBRELLA AND NO SWITCH. The city picker needs both
 * because 24 labels do not fit on a screen and "anywhere in India" is a different
 * kind of answer from a list. Three checkboxes need neither, and inventing an
 * "any arrangement" switch would add a second way to say what ticking all three
 * already says — the sentinel-inside-an-array mistake db/schema.sql argues
 * against for all_india, arriving as a control instead of as a column.
 *
 * WHY THE ON-SITE HINT IS THE LONG ONE. lib/match.DEFAULT_WORK_MODE reads a NULL
 * jobs.work_mode as onsite, and NULL is the state of every posting whose
 * description nobody has read — the large majority of the board, permanently,
 * because a description is only fetched for a job some filter already wants. So
 * unticking On-site is by far the most destructive thing on this panel, and it
 * does not look it. Saying so on the screen is the same courtesy the "99% of
 * postings state no minimum" hint pays one panel up.
 *
 * The remote hint states the relaxation because it is genuinely surprising: a
 * remote job matches anywhere in India, which means ticking Remote can widen the
 * digest past the cities the person just picked. Better read than discovered.
 */
export function WorkModePicker({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  function toggle(value: string, on: boolean) {
    onChange(on ? [...selected, value] : selected.filter((m) => m !== value));
  }

  const none = selected.length === 0;

  return (
    <div className="panel">
      <h2>
        Work arrangement <span className="muted small">({selected.length} of 3)</span>
      </h2>
      <p className="hint">
        We read each job&rsquo;s description to work out whether it is on-site, hybrid or fully
        remote. A job only reaches you if its arrangement is one you tick here.
      </p>

      {WORK_MODE_OPTIONS.map((option) => (
        <div key={option.value} style={{ marginTop: 12 }}>
          <label className="switch">
            <input
              type="checkbox"
              checked={selected.includes(option.value)}
              onChange={(e) => toggle(option.value, e.target.checked)}
              aria-label={option.label}
            />
            <span>
              <strong>{option.label}</strong>
            </span>
          </label>
          <p className="hint">{option.hint}</p>
        </div>
      ))}

      {none ? (
        <p className="msg bad">
          Nothing is ticked, so no job can match at all. Pick at least one.
        </p>
      ) : null}
    </div>
  );
}
