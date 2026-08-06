"use client";

import { useState } from "react";

import { KEYWORD_CAP } from "@/lib/labels";

/**
 * The keyword list: removable chips, and a box you type into.
 *
 * WHY THIS IS NOT THE GENERIC ChipList NEXT DOOR. Exclusions still use that one
 * and should. This list is different in three ways that are each a rule rather
 * than a style: it is capped, it is filled by a machine reading a resume so it
 * has an over-cap state the other never reaches, and it is the one list whose
 * contents a person may have never typed. Folding those into the shared
 * component would put three conditionals into a component whose whole value is
 * that it has none.
 *
 * THERE IS NO AUTOCOMPLETE, AND ITS ABSENCE IS DELIBERATE. Typing "gi" offers
 * nothing — not git, not github, not a dropdown, not a ghost completion. Two
 * reasons. The honest one: a suggestion list is a claim about which skills are
 * real, and this app has no business making it — lib/skills.ts is a vocabulary
 * for READING a resume, and turning it round into a menu would quietly tell
 * somebody their language is not a language. The practical one: these strings
 * are matched against job titles, so an unusual term someone types on purpose is
 * exactly the entry most likely to be the reason they are here.
 *
 * Nothing is filtered either. Any string a person types is accepted, stored and
 * matched. The only transformation is the one correctness requires — see below.
 */
export function KeywordChips({
  values,
  onChange,
}: {
  values: string[];
  onChange: (next: string[]) => void;
}) {
  const [draft, setDraft] = useState("");

  const over = values.length - KEYWORD_CAP;
  const full = values.length >= KEYWORD_CAP;

  function add() {
    // Lowercased and whitespace-collapsed, and that is NOT a content filter —
    // it is the same normalisation lib/preferences.cleanList applies on the
    // server and the reason it applies it: notify/match.py compares against a
    // normalised, lowercased title, so a keyword stored as "Python" would match
    // nothing at all. Doing it here as well means the chip on screen is exactly
    // the string that will be stored and compared.
    const value = draft.replace(/\s+/g, " ").trim().toLowerCase();
    if (!value) return;
    // De-duplication is case-insensitive by construction: both sides of this
    // comparison have been through the same lowercasing.
    if (values.includes(value)) {
      setDraft("");
      return;
    }
    if (values.length >= KEYWORD_CAP) return;
    onChange([...values, value]);
    setDraft("");
  }

  return (
    <div className="panel">
      <h2>
        Keywords{" "}
        <span className={over > 0 ? "count-over" : "muted small"}>
          ({values.length} of {KEYWORD_CAP})
        </span>
      </h2>
      <p className="hint">
        A job title must contain at least one of these to reach you. Upload a resume above to
        fill this in, or type your own — anything you type is kept exactly as written.
      </p>

      {over > 0 ? (
        <p className="msg bad">
          {values.length} keywords is over the limit of {KEYWORD_CAP}. Remove {over} to save.
          Until you do, your saved filter keeps running unchanged — nothing here has been
          altered.
        </p>
      ) : null}

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
        <p className="msg bad">
          An empty keyword list matches nothing — every digest would be silent.
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
          placeholder={full ? `Remove one to add another` : "e.g. backend engineer"}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Add a keyword"
          disabled={full}
          // The three ways a browser volunteers a suggestion, all off. autoComplete
          // alone is not enough: Chrome still offers form history, and mobile
          // keyboards still offer predictive text over the field.
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="none"
          spellCheck={false}
          list={undefined}
        />
        <div className="fixed">
          <button type="submit" disabled={!draft.trim() || full}>
            Add
          </button>
        </div>
      </form>
    </div>
  );
}
