"use client";

import { useState } from "react";

// From lib/labels, never lib/preferences: the latter imports postgres.js, and
// importing a VALUE from it here would pull the driver into the client bundle.
import {
  CITY_LABELS,
  KEYWORD_CAP,
  LEVEL_LABELS,
  type Preferences,
  type StoredResume,
} from "@/lib/labels";
import { KeywordChips } from "./KeywordChips";
import { ResumePanel } from "./ResumePanel";

/**
 * Editor for the one preferences row. Replaces editing the filter by hand in
 * a script or a psql session.
 *
 * Everything is staged locally and written by one Save, rather than each chip
 * being its own PUT. With a single row that means the thing on screen is always
 * a coherent filter: you can drop a keyword and add its replacement without the
 * digest briefly running against the half-edited version in between.
 *
 * THE RESUME IS THE ONE THING THAT DOES NOT WAIT FOR SAVE, and the asymmetry is
 * honest rather than accidental. Uploading a file IS the act of storing it —
 * there is no version of "staged locally" for a 300 KB PDF that has to reach a
 * bucket. What the upload produces here is a list of SUGGESTED keywords, and
 * those are staged like everything else: they land in the chips, a person edits
 * them, and Save writes them. So the screen has two commitments with two
 * different moments, and each says which it is.
 *
 * KEYWORDS MOVED OUT OF THE GENERIC ChipList IN SLICE 1. There is exactly one
 * keyword editor on this page, because two controls writing preferences.keywords
 * with different caps is a split-brain waiting to be discovered by whichever one
 * saves second.
 */
export function PreferencesEditor({
  initial,
  resume,
}: {
  initial: Preferences;
  resume: StoredResume | null;
}) {
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

  const overCap = keywords.length > KEYWORD_CAP;

  /**
   * Fold a resume's suggestions into the list already on screen.
   *
   * MERGE, NOT REPLACE, and the existing chips win the ties. Somebody who typed
   * six keywords by hand and then uploaded a resume has not asked for those six
   * to be thrown away — and replacing would do exactly that, silently, in
   * response to an action about a file. So the current list keeps its order and
   * its place, and suggestions fill whatever room is left.
   *
   * STOPS AT THE CAP RATHER THAN GOING OVER IT. Suggestions are the one source
   * that can produce more than a person asked for, so this is the one place
   * where dropping the excess is right: nothing a human chose is lost, and the
   * count says how many were left out. That is the opposite of the rule on the
   * server, where truncating a list somebody submitted would be rewriting their
   * filter — see the note in api/preferences/route.ts.
   */
  function mergeSuggestions(suggested: string[]) {
    setKeywords((current) => {
      const merged = [...current];
      for (const term of suggested) {
        if (merged.length >= KEYWORD_CAP) break;
        if (!merged.includes(term)) merged.push(term);
      }
      return merged;
    });
  }

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
      <ResumePanel initial={resume} onKeywords={mergeSuggestions} />

      <KeywordChips values={keywords} onChange={setKeywords} />

      <div className="panel">
        <h2>Cities</h2>
        <p className="hint">
          Which <code>city_label</code> values may be sent. <code>india-unclear</code> now means
          only &ldquo;we could not tell&rdquo; &mdash; &ldquo;Remote&rdquo;, bare
          &ldquo;India&rdquo;, or a state that contains one of the three cities. A posting that
          names an Indian city we did not ask for (Lucknow, Noida, Mohali) is{" "}
          <code>other</code>, the same as London. It is off by default because it used to be on
          and leaked 14 wrong-city jobs into one digest; turning it back on costs noise, not
          correctness.
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
        {/*
          * Disabled over the cap as well as when clean. The server refuses it
          * anyway — that is where the rule lives — so this is about telling
          * somebody why the button is not working before they press it, rather
          * than about enforcement. The label says the number, because a greyed
          * button with no reason is the thing people file bugs about.
          */}
        <button className="primary" onClick={save} disabled={busy || !dirty || overCap}>
          {busy
            ? "Saving…"
            : overCap
              ? `Remove ${keywords.length - KEYWORD_CAP} keyword${
                  keywords.length - KEYWORD_CAP === 1 ? "" : "s"
                } to save`
              : dirty
                ? "Save preferences"
                : "No changes"}
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
