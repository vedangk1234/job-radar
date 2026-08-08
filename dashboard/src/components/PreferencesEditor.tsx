"use client";

import { useRouter } from "next/navigation";
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
 *
 * redirectOnSave IS THE WHOLE OF WHAT SLICE 1b ADDED, and it is one optional
 * string rather than a mode. /onboarding passes "/" because a successful save
 * there is the act that sets onboarding_completed, which is the act that opens
 * the rest of the app — leaving somebody on the setup screen afterwards would
 * mean the gate had lifted with nothing on the page saying so. /preferences
 * passes nothing and is unchanged: navigating away from a settings screen when
 * you press Save is how an edit you meant to keep tweaking gets lost.
 *
 * A path, not a callback, because this is a client component rendered by a
 * server one and a function is not serialisable across that boundary.
 */
export function PreferencesEditor({
  initial,
  resume,
  redirectOnSave,
}: {
  initial: Preferences;
  resume: StoredResume | null;
  redirectOnSave?: string;
}) {
  const router = useRouter();
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
   * A parsed resume REPLACES the keyword list outright.
   *
   * REPLACE, NOT MERGE, and it used to be the other way round. Merging read
   * well — nobody who typed six keywords by hand asked for them to be thrown
   * away — but it made the one action people actually perform impossible to
   * perform: uploading a NEW resume because the old one was wrong. The chips
   * are meant to describe the resume on file, and a fold left the previous
   * resume's terms sitting in the list with nothing on screen saying which
   * document they came from. Observed: a replacement resume reporting "13
   * skills found" left the panel showing 25 of 25, twelve of them the old
   * resume's. So the list becomes exactly what was read out of the file that
   * was just uploaded.
   *
   * ONLY ON A SUCCESSFUL PARSE, which is a property of the caller rather than
   * of this function: ResumePanel throws before calling onKeywords if the route
   * refused the file, so a rejected upload cannot clear a list. That ordering
   * is the whole safety argument — a failed replace that wiped the filter would
   * be worse than the merge this replaces.
   *
   * STILL CAPPED, for the same reason as before: suggestions are the one source
   * that can produce more than a person asked for. The server already caps at
   * extraction (api/resume/route.ts passes KEYWORD_CAP to extractSkills), and
   * this restates it rather than trusting it, because the over-cap state is
   * unsaveable and arriving in it through a file upload gives a person no way
   * to see why. Truncating here is not the same act as truncating a list
   * somebody SUBMITTED — see the note in api/preferences/route.ts.
   *
   * A person edits freely afterwards: add, remove and typing are untouched, and
   * nothing is written until Save.
   */
  function replaceWithSuggestions(suggested: string[]) {
    setKeywords(suggested.slice(0, KEYWORD_CAP));
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

      // ONLY ON A SUCCESSFUL SAVE, and only because the row now says so. The
      // response carries onboarding_completed straight from the UPDATE's
      // RETURNING, so this navigates on what the database recorded rather than
      // on the request having been sent. refresh() first: the destination is a
      // server component behind the same gate, and it has to re-read the flag
      // that just changed rather than serve a cached render from before it did.
      if (redirectOnSave && data.preferences.onboarding_completed) {
        router.refresh();
        router.replace(redirectOnSave);
        return;
      }
    } catch (err) {
      setMessage({ kind: "bad", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <ResumePanel initial={resume} onKeywords={replaceWithSuggestions} />

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
