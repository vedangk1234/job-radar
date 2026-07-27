"use client";

import { useState } from "react";

// From lib/labels, never lib/preferences: the latter imports postgres.js, and
// importing a VALUE from it here would pull the driver into the client bundle.
import { CITY_LABELS, LEVEL_LABELS, type Preferences } from "@/lib/labels";

/**
 * Editor for the one preferences row. Replaces editing the filter by hand in
 * a script or a psql session.
 *
 * Everything is staged locally and written by one Save, rather than each chip
 * being its own PUT. With a single row that means the thing on screen is always
 * a coherent filter: you can drop a keyword and add its replacement without the
 * digest briefly running against the half-edited version in between.
 */
export function PreferencesEditor({ initial }: { initial: Preferences }) {
  const [cities, setCities] = useState<string[]>(initial.cities);
  const [keywords, setKeywords] = useState<string[]>(initial.keywords);
  const [exclusions, setExclusions] = useState<string[]>(initial.exclusions);
  const [maxLevel, setMaxLevel] = useState<number>(initial.max_level);
  const [updatedAt, setUpdatedAt] = useState<string>(initial.updated_at);

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);

  const dirty =
    JSON.stringify([cities, keywords, exclusions, maxLevel]) !==
    JSON.stringify([initial.cities, initial.keywords, initial.exclusions, initial.max_level]);

  async function save() {
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch("/api/preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cities, keywords, exclusions, max_level: maxLevel }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      setCities(data.preferences.cities);
      setKeywords(data.preferences.keywords);
      setExclusions(data.preferences.exclusions);
      setMaxLevel(data.preferences.max_level);
      setUpdatedAt(data.preferences.updated_at);
      setMessage({ kind: "ok", text: data.message });
    } catch (err) {
      setMessage({ kind: "bad", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="panel">
        <h2>Cities</h2>
        <p className="hint">
          Which <code>city_label</code> values may be sent. <code>india-unclear</code> is the
          catch-all for a posting that is Indian but does not name one of the three cities —
          dropping it loses real jobs.
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 14 }}>
          {CITY_LABELS.map((label) => (
            <label key={label} className="switch">
              <input
                type="checkbox"
                checked={cities.includes(label)}
                onChange={(e) =>
                  setCities((cs) =>
                    e.target.checked
                      ? [...cs, label]
                      : cs.filter((c) => c !== label),
                  )
                }
              />
              <span className="mono">{label}</span>
            </label>
          ))}
        </div>
        {!cities.length ? (
          <p className="msg bad">An empty city list matches nothing. Pick at least one.</p>
        ) : null}
      </div>

      <ChipList
        title="Keywords"
        hint="A title must match at least ONE of these to be sent. Matching is on the normalised title, so entries are stored lowercase."
        values={keywords}
        onChange={setKeywords}
        placeholder="e.g. backend engineer"
        emptyIsError
        emptyMessage="An empty keyword list matches nothing — every digest would be silent."
      />

      <ChipList
        title="Exclusions"
        hint="A title matching ANY of these is dropped, even if a keyword matched. Empty is allowed and means nothing is excluded by title."
        values={exclusions}
        onChange={setExclusions}
        placeholder="e.g. intern"
      />

      <div className="panel">
        <h2>Max level</h2>
        <p className="hint">
          Titles that state a seniority above this are dropped. A title that states no level at
          all counts as 2, so lowering this below 2 filters out most ordinary postings.
        </p>
        <select
          value={maxLevel}
          onChange={(e) => setMaxLevel(Number(e.target.value))}
          style={{ maxWidth: 320 }}
        >
          {Object.entries(LEVEL_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>

      <div
        className="panel"
        style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}
      >
        <button className="primary" onClick={save} disabled={busy || !dirty}>
          {busy ? "Saving…" : dirty ? "Save preferences" : "No changes"}
        </button>
        <span className="muted small">
          Last saved {new Date(updatedAt).toLocaleString()}
        </span>
      </div>

      {message ? <p className={`msg ${message.kind}`}>{message.text}</p> : null}
    </>
  );
}

/** Add/remove list of short strings. Used three times, so it is one component. */
function ChipList({
  title,
  hint,
  values,
  onChange,
  placeholder,
  emptyIsError = false,
  emptyMessage,
}: {
  title: string;
  hint: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
  emptyIsError?: boolean;
  emptyMessage?: string;
}) {
  const [draft, setDraft] = useState("");

  function add() {
    // Lowercased and trimmed here as well as on the server, so the chip you see
    // is exactly the string that will be stored and compared against.
    const value = draft.replace(/\s+/g, " ").trim().toLowerCase();
    if (!value) return;
    if (!values.includes(value)) onChange([...values, value]);
    setDraft("");
  }

  return (
    <div className="panel">
      <h2>
        {title} <span className="muted small">({values.length})</span>
      </h2>
      <p className="hint">{hint}</p>

      {values.length ? (
        <ul className="chips">
          {values.map((value) => (
            <li key={value}>
              {value}
              <button
                className="linky"
                onClick={() => onChange(values.filter((v) => v !== value))}
                aria-label={`Remove ${value}`}
                title={`Remove ${value}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className={emptyIsError ? "msg bad" : "chips empty-note"}>
          {emptyMessage ?? "None."}
        </p>
      )}

      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input
          type="text"
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          aria-label={`Add to ${title}`}
        />
        <div className="fixed">
          <button type="submit" disabled={!draft.trim()}>
            Add
          </button>
        </div>
      </form>
    </div>
  );
}
