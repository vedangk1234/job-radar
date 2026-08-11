"use client";

import { useMemo, useRef, useState } from "react";

// Types only — erased at compile time, so no database module reaches the bundle.
// The one VALUE this file imports is FREE_WATCHLIST_CAP's sibling in lib/labels,
// which imports nothing at all by design. See lib/labels.ts.
import type { CompanyRow } from "@/lib/companies";

/**
 * THE COMPANY PICKER — browse the shared catalog, choose the companies YOU watch.
 *
 * The control CLAUDE.md has been describing as "the next slice" since Phase 1c,
 * and the one the tracked toggle removed in Slice 1a was never a stand-in for.
 * The distinction is the whole point of the screen:
 *
 *   companies.tracked   the CRAWLER's flag. One shared value for the install:
 *                       whether this board is fetched at all. Not editable here,
 *                       not editable anywhere in the web app, and shown as a pill
 *                       so an untracked company's stale jobs explain themselves.
 *   user_companies      YOUR watch list. A row per (you, company). Nobody else's
 *                       digest changes when you add or remove one.
 *
 * WHAT AN ADD ACTUALLY DOES, because it is more than it looks. The digest queue
 * is "open, in a company you watch, not back catalogue, not yet sent to you", so
 * the instant a pair exists every job that company already holds is in your
 * queue. The server therefore records that company's stored jobs as already-seen
 * for you, in the same transaction as the pair — and returns how many, which is
 * what the confirmation line under a freshly-added company reports. A person who
 * was not told would expect two hundred postings in the morning and read a
 * correct three-job digest as broken.
 *
 * OPTIMISTIC, LIKE EVERY OTHER CONTROL IN THIS APP, and rolled back on failure
 * the way CompanyList's category select is. The round trip to Tokyo is long
 * enough that a button which waits for it gets pressed twice — and pressing add
 * twice is precisely the case the server's ON CONFLICT makes into a no-op, so the
 * failure mode of being wrong here is a message rather than a duplicate row.
 *
 * THE ORDER IS FIXED AT FIRST RENDER. Watched companies sort to the top so the
 * list reads as "yours, then the rest" — but re-sorting on every toggle would
 * make a row jump out from under the cursor the moment it was clicked, which is
 * the same reason CompanyList never recomputes its order after mount.
 */

/** The columns this picker needs. `CompanyRow` satisfies it structurally. */
export type PickerCompany = Pick<
  CompanyRow,
  "id" | "name" | "ats" | "token" | "category" | "tracked" | "open_jobs"
>;

export function WatchlistPicker({
  catalog,
  initialWatched,
  cap,
  onWatchedChange,
}: {
  catalog: PickerCompany[];
  initialWatched: number[];
  /** null = no limit. From the account's plan; see lib/labels.watchlistCapForPlan. */
  cap: number | null;
  /** Told about every change, so a parent (onboarding) can gate its Save on it. */
  onWatchedChange?: (ids: number[]) => void;
}) {
  const [watched, setWatched] = useState<number[]>(initialWatched);
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // Captured once, at mount — the sort key, deliberately frozen. See the header.
  const initiallyWatched = useRef(new Set(initialWatched));

  const ordered = useMemo(() => {
    return [...catalog].sort((a, b) => {
      const mine = Number(initiallyWatched.current.has(b.id)) - Number(initiallyWatched.current.has(a.id));
      return mine || a.name.localeCompare(b.name);
    });
  }, [catalog]);

  const needle = query.replace(/\s+/g, " ").trim().toLowerCase();
  const shown = needle
    ? ordered.filter((c) =>
        [c.name, c.token, c.ats, c.category ?? ""].some((field) =>
          field.toLowerCase().includes(needle),
        ),
      )
    : ordered;

  const watchedSet = new Set(watched);
  const full = cap !== null && watched.length >= cap;

  function apply(next: number[]) {
    setWatched(next);
    onWatchedChange?.(next);
  }

  /**
   * Add or remove one company.
   *
   * The response is the authority, not the click: it carries the server's own
   * `company_ids`, so a list that drifted (another tab, a cap refused elsewhere)
   * is corrected rather than accumulating.
   */
  async function toggle(company: PickerCompany, watch: boolean) {
    const before = watched;
    setPending(company.id);
    setError(null);
    setNote(null);
    apply(watch ? [...watched, company.id] : watched.filter((id) => id !== company.id));

    try {
      const res = await fetch("/api/watchlist", {
        method: watch ? "POST" : "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company_id: company.id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);

      apply(data.company_ids as number[]);
      setNote(
        watch
          ? `${company.name}: ${data.message}`
          : `${company.name}: ${data.message}`,
      );
    } catch (err) {
      apply(before);
      setError(
        `Could not ${watch ? "watch" : "stop watching"} ${company.name}: ` +
          `${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="panel">
      <h2>
        Companies you watch{" "}
        <span className="muted small">
          ({watched.length}
          {cap === null ? "" : ` of ${cap}`})
        </span>
      </h2>
      <p className="hint">
        Only these companies&rsquo; jobs reach your digest and your Home screen. Everything in
        the catalog is crawled hourly for everybody either way &mdash; watching one is how you
        ask to hear about it, not how it gets fetched. When you add a company we mark the jobs
        it <strong>already</strong> has as seen, so your first email is what arrives from now on rather
        than its back catalogue.
      </p>

      {cap !== null && watched.length > cap ? (
        <p className="msg warn">
          You watch {watched.length} companies and the limit on your plan is {cap}. Nothing has
          been removed &mdash; every one of them still counts and still emails you &mdash; but
          you cannot add another until you remove some.
        </p>
      ) : null}

      {watched.length === 0 ? (
        <p className="msg bad">
          You are not watching any company yet, so nothing can match: your digest would be empty
          and Home would be blank. Pick at least one below.
        </p>
      ) : null}

      {error ? <p className="msg bad">{error}</p> : null}
      {note ? <p className="msg ok">{note}</p> : null}

      <div className="row" style={{ marginBottom: 10 }}>
        <input
          type="search"
          value={query}
          placeholder="Search the catalog — name, board token, ATS, category"
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search companies"
        />
      </div>

      {shown.length === 0 ? (
        <p className="muted small">
          No company in the catalog matches &ldquo;{query}&rdquo;. Add its board on the Add tab
          first &mdash; that puts it in the shared catalog, which is a different act from
          watching it.
        </p>
      ) : (
        <ul className="companies">
          {shown.map((company) => {
            const mine = watchedSet.has(company.id);
            const busy = pending === company.id;
            return (
              <li key={company.id} className={`company${mine ? " watched" : ""}`}>
                <div className="company-head">
                  <span className="stack" style={{ flex: 1 }}>
                    <span className="company-name">
                      {company.name}
                      <span className="tag">{company.ats}</span>
                      {mine ? <span className="tag on">watching</span> : null}
                      {company.tracked ? null : (
                        <span className="tag off" title="Not being crawled — its jobs are stale.">
                          off
                        </span>
                      )}
                    </span>
                    <span className="muted small mono">
                      {company.token} &middot; {company.open_jobs} open job
                      {company.open_jobs === 1 ? "" : "s"}
                    </span>
                  </span>
                  <div className="company-controls">
                    <button
                      type="button"
                      className={mine ? "small" : "primary small"}
                      disabled={busy || (!mine && full)}
                      onClick={() => toggle(company, !mine)}
                      aria-label={`${mine ? "Stop watching" : "Watch"} ${company.name}`}
                      title={
                        !mine && full
                          ? `You are already watching ${cap}, the limit on your plan.`
                          : undefined
                      }
                    >
                      {busy ? "…" : mine ? "Remove" : full ? "Limit reached" : "Watch"}
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <p className="muted small" style={{ marginTop: 10 }}>
        {cap === null
          ? "Your plan has no limit on how many companies you can watch."
          : `Free accounts watch up to ${cap} companies. The limit is checked on the server, so it holds however the request is made.`}
      </p>
    </div>
  );
}
