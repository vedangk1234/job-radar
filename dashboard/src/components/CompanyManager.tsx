"use client";

import { useMemo, useState } from "react";

import type { CompanyCard } from "@/lib/companies";

/**
 * The crawl-selection screen: every company in the catalog, and which of them the
 * hourly crawl fetches.
 *
 * WHY IT IS NOT CompanyList. CompanyList is the Home/Jobs card that expands to a
 * per-company job list, fetched on open — heavy machinery this screen does not
 * want. This is a flat, paginated table whose one job is turning `tracked` on and
 * off across ~700 rows, so it is its own component. Both write the same shared
 * flag through the same PATCH /api/companies/:id { tracked } endpoint; the cap and
 * the transaction live on the server (lib/companies.setTracked), so neither client
 * can exceed the limit even if this one's disabled-button arithmetic were wrong.
 *
 * THE COUNT IS LIVE AND INSTALL-WIDE. `trackedNow` starts from the server's count
 * over the whole catalog and moves ±1 as toggles resolve, so "N / cap" is honest
 * even though only fifty rows are on screen. A refused add (409 at the cap) rolls
 * its optimistic +1 back, so the counter never shows more than are really tracked.
 */

type ManagerSort = "relevant" | "jobs" | "az" | "za";

const SORTS: ReadonlyArray<{ value: ManagerSort; label: string }> = [
  { value: "relevant", label: "Most relevant" },
  { value: "jobs", label: "Most jobs" },
  { value: "az", label: "A–Z" },
  { value: "za", label: "Z–A" },
];

const PAGE_SIZE = 50;

export function CompanyManager({
  initial,
  cap,
  trackedCount,
}: {
  initial: CompanyCard[];
  /** TRACKED_CAP — the install-wide crawl limit. null means no limit. */
  cap: number | null;
  trackedCount: number;
}) {
  const [rows, setRows] = useState(initial);
  const [trackedNow, setTrackedNow] = useState(trackedCount);
  const [sort, setSort] = useState<ManagerSort>("relevant");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);

  const canTrackMore = cap === null || trackedNow < cap;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q
      ? rows.filter(
          (c) => c.name.toLowerCase().includes(q) || c.token.toLowerCase().includes(q),
        )
      : rows;
    const byName = (a: CompanyCard, b: CompanyCard) => a.name.localeCompare(b.name);
    const next = [...list];
    if (sort === "az") return next.sort(byName);
    if (sort === "za") return next.sort((a, b) => byName(b, a));
    if (sort === "jobs") return next.sort((a, b) => b.open_jobs - a.open_jobs || byName(a, b));
    return next.sort((a, b) => b.match_jobs - a.match_jobs || byName(a, b));
  }, [rows, query, sort]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const clampedPage = Math.min(page, pageCount - 1);
  const shown = filtered.slice(clampedPage * PAGE_SIZE, clampedPage * PAGE_SIZE + PAGE_SIZE);

  async function toggle(company: CompanyCard) {
    const next = !company.tracked;
    if (next && !canTrackMore) return; // button is disabled; belt and braces

    const before = rows;
    setPendingId(company.id);
    setError(null);
    setRows((rs) => rs.map((r) => (r.id === company.id ? { ...r, tracked: next } : r)));
    setTrackedNow((n) => n + (next ? 1 : -1));

    try {
      const res = await fetch(`/api/companies/${company.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tracked: next }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      const saved = data.company as CompanyCard;
      setRows((rs) => rs.map((r) => (r.id === saved.id ? saved : r)));
    } catch (err) {
      setRows(before);
      setTrackedNow((n) => n - (next ? 1 : -1));
      setError(
        `Could not ${next ? "start" : "stop"} crawling ${company.name}: ` +
          `${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setPendingId(null);
    }
  }

  function changeSort(value: ManagerSort) {
    setSort(value);
    setPage(0);
  }

  function changeQuery(value: string) {
    setQuery(value);
    setPage(0);
  }

  return (
    <>
      <div className="manage-bar">
        <span className="manage-count">
          <strong>{trackedNow}</strong>
          {cap === null ? "" : ` / ${cap}`} crawled
        </span>
        <input
          type="search"
          className="manage-search"
          placeholder="Search companies…"
          value={query}
          aria-label="Search companies"
          onChange={(e) => changeQuery(e.target.value)}
        />
        <label className="sort-select">
          <span className="muted small">Sort</span>
          <select
            value={sort}
            aria-label="Sort companies"
            onChange={(e) => changeSort(e.target.value as ManagerSort)}
          >
            {SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error ? <p className="msg bad">{error}</p> : null}

      {cap !== null && trackedNow >= cap ? (
        <p className="msg warn">
          You are crawling the maximum of {cap} companies. Untrack one before adding another.
        </p>
      ) : null}

      <ul className="manage-list">
        {shown.map((company) => {
          const atCapUntracked = !company.tracked && !canTrackMore;
          return (
            <li key={company.id} className={`manage-row${company.tracked ? "" : " untracked"}`}>
              <span className="manage-name">
                {company.name}
                <span className="tag">{company.ats}</span>
              </span>
              <span className="manage-nums">
                <span className="muted small" title="Open jobs matching your preferences">
                  {company.match_jobs} for you
                </span>
                <span className="muted small" title="All open jobs stored">
                  {company.open_jobs} open
                </span>
              </span>
              <button
                type="button"
                className={`track-toggle tag ${company.tracked ? "on" : "off"}`}
                aria-pressed={company.tracked}
                aria-label={`${company.tracked ? "Stop crawling" : "Crawl"} ${company.name}`}
                disabled={pendingId === company.id || atCapUntracked}
                title={
                  company.tracked
                    ? "Crawled hourly. Click to stop."
                    : atCapUntracked
                      ? "The crawl limit is full. Untrack a company first."
                      : "Not crawled. Click to add it to the hourly crawl."
                }
                onClick={() => toggle(company)}
              >
                {company.tracked ? "tracked" : "off"}
              </button>
            </li>
          );
        })}
      </ul>

      {pageCount > 1 ? (
        <div className="manage-pager">
          <button
            type="button"
            className="small"
            disabled={clampedPage === 0}
            onClick={() => setPage(clampedPage - 1)}
          >
            ← Prev
          </button>
          <span className="muted small">
            Page {clampedPage + 1} of {pageCount} · {filtered.length} compan
            {filtered.length === 1 ? "y" : "ies"}
          </span>
          <button
            type="button"
            className="small"
            disabled={clampedPage >= pageCount - 1}
            onClick={() => setPage(clampedPage + 1)}
          >
            Next →
          </button>
        </div>
      ) : null}
    </>
  );
}
