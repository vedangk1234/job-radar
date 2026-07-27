"use client";

import { useCallback, useId, useState } from "react";

import type { CompanyCard } from "@/lib/companies";
import type { JobRow } from "@/lib/jobs";
import { CATEGORIES } from "@/lib/labels";
import { descriptionHint } from "@/lib/match";

/**
 * The company list, and the one collapsible row both screens are made of.
 *
 * ONE COMPONENT, TWO PAGES
 * ------------------------
 * Home and Jobs differ by a single prop. `mode` picks which job set a row
 * fetches when it opens — the digest filter or everything — and picks the two
 * or three details each job shows. Nothing else about the two screens is
 * different, so nothing else is duplicated: one expand animation to get right,
 * one loading state, one empty message, one place where a broken link would be.
 *
 * WHY THE JOBS ARE FETCHED AND NOT PASSED IN
 * ------------------------------------------
 * The server already knows every job it would need. Sending them all with the
 * page would be ~790 rows of JSON in the HTML to render twelve numbers, and
 * eleven of those twelve companies stay collapsed. So the page ships counts,
 * and a row asks for its own jobs the first time it is opened — once; the
 * answer is kept, so reopening is instant and closing costs nothing.
 */

export type Mode = "matches" | "all";

export function CompanyList({ initial, mode }: { initial: CompanyCard[]; mode: Mode }) {
  const [rows, setRows] = useState(initial);
  const [error, setError] = useState<string | null>(null);

  // Replace one card in place. Order is never recomputed after the first
  // render: a row must not jump to a different position under the cursor
  // because the tag next to it was edited.
  const replace = useCallback((next: CompanyCard) => {
    setRows((rs) => rs.map((r) => (r.id === next.id ? next : r)));
  }, []);

  if (!rows.length) {
    return (
      <p className="msg warn">
        No companies yet. Add one on the Add tab, or run{" "}
        <code>python scripts/seed_companies.py</code>.
      </p>
    );
  }

  return (
    <>
      {error ? <p className="msg bad">{error}</p> : null}
      <ul className="companies">
        {rows.map((company) => (
          <CompanyRow
            key={company.id}
            company={company}
            mode={mode}
            onPatched={replace}
            onError={setError}
          />
        ))}
      </ul>
    </>
  );
}

/**
 * One company: a header that is always visible, and a job list that is not.
 *
 * The header is deliberately NOT one big click target. The disclosure button
 * covers the name and the counts; the category tag and the tracked switch sit
 * outside it, as siblings. Nesting a <select> inside a <button> is invalid HTML
 * and behaves accordingly — the row would open every time the tag was changed.
 */
function CompanyRow({
  company,
  mode,
  onPatched,
  onError,
}: {
  company: CompanyCard;
  mode: Mode;
  onPatched: (next: CompanyCard) => void;
  onError: (message: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [jobs, setJobs] = useState<JobRow[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const panelId = useId();

  const shown = mode === "matches" ? company.match_jobs : company.open_jobs;

  async function toggleOpen() {
    const next = !open;
    setOpen(next);
    if (!next || jobs !== null || loading) return;

    setLoading(true);
    setLoadError(null);
    try {
      const res = await fetch(`/api/companies/${company.id}/jobs?filter=${mode}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      setJobs(data.jobs as JobRow[]);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  /**
   * One PATCH for both editable fields. Optimistic — the round trip to Tokyo is
   * long enough that a switch which waits for it feels broken and gets tapped
   * twice — and it rolls back to the card we started from if the write fails.
   */
  async function patch(body: { tracked?: boolean; category?: string }) {
    const before = company;
    setPending(true);
    onError(null);
    onPatched({ ...company, ...body });

    try {
      const res = await fetch(`/api/companies/${company.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      onPatched(data.company as CompanyCard); // trust the server over the guess
    } catch (err) {
      onPatched(before);
      onError(
        `Could not update ${before.name}: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <li className={`company${company.tracked ? "" : " untracked"}`}>
      <div className="company-head">
        <button
          type="button"
          className="disclose"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={toggleOpen}
        >
          <span className="chev" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
          <span className="stack">
            <span className="company-name">
              {company.name}
              <span className="tag">{company.ats}</span>
              {company.india_scoped ? (
                <span
                  className="tag"
                  title="Filtered at source: this company's rows are deliberately India-only and therefore partial."
                >
                  india-scoped
                </span>
              ) : null}
            </span>
            <span className="muted small mono">{company.token}</span>
          </span>
          <span className="counts">
            {mode === "matches" ? (
              <span className={`count${company.match_jobs ? " hit" : ""}`}>
                <strong className="num">{company.match_jobs}</strong>
                <span className="muted small">for you</span>
              </span>
            ) : null}
            <span className="count">
              <strong className="num">{company.open_jobs}</strong>
              <span
                className="muted small"
                title={`${company.total_jobs} rows stored in all, ${
                  company.total_jobs - company.open_jobs
                } of them closed`}
              >
                total jobs
              </span>
            </span>
          </span>
        </button>

        <div className="company-controls">
          <label className="cat">
            <span className="sr-only">Category for {company.name}</span>
            <select
              value={company.category ?? "unknown"}
              disabled={pending}
              onChange={(e) => patch({ category: e.target.value })}
            >
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label className="switch">
            <input
              type="checkbox"
              checked={company.tracked}
              disabled={pending}
              onChange={() => patch({ tracked: !company.tracked })}
              aria-label={`Track ${company.name}`}
            />
            <span className={company.tracked ? "tag on" : "tag off"}>
              {company.tracked ? "tracked" : "off"}
            </span>
          </label>
        </div>
      </div>

      {company.tracked ? null : (
        <p className="untracked-note muted small">
          Not being crawled. These {company.open_jobs} job
          {company.open_jobs === 1 ? "" : "s"} are whatever was stored the last time it was
          tracked, and nothing new will arrive until the switch goes back on.
        </p>
      )}

      <div id={panelId} hidden={!open} className="company-body">
        {loading ? <p className="muted small">Loading…</p> : null}
        {loadError ? <p className="msg bad">Could not load jobs: {loadError}</p> : null}
        {!loading && !loadError && jobs !== null ? (
          jobs.length ? (
            <ul className="jobs">
              {jobs.map((job) => (
                <JobLine key={job.canonical_id} job={job} mode={mode} />
              ))}
            </ul>
          ) : (
            <p className="muted small">
              {mode === "matches"
                ? "No jobs matching your keywords."
                : "No open jobs stored for this company."}
            </p>
          )
        ) : null}
        {!loading && !loadError && jobs !== null && jobs.length !== shown ? (
          <p className="muted small">
            The row said {shown}; the database now says {jobs.length}. The crawler has run since
            this page loaded — reload to refresh the counts.
          </p>
        ) : null}
      </div>
    </li>
  );
}

/**
 * One job. The title IS the apply link: it is what a person wants to click, and
 * making them hunt for a separate "apply" affordance next to it would be
 * decoration standing between them and the posting.
 *
 * `rel="noopener noreferrer"` on every one of them. `noopener` because a tab
 * opened with target=_blank can otherwise reach back through window.opener and
 * navigate this page somewhere else; `noreferrer` because which dashboard sent
 * a click is nobody's business but this dashboard's.
 */
function JobLine({ job, mode }: { job: JobRow; mode: Mode }) {
  const hint = descriptionHint(job);

  return (
    <li className="job">
      <a href={job.url} target="_blank" rel="noopener noreferrer">
        {job.title}
      </a>
      <div className="job-meta">
        <span className="tag">{job.city ?? "—"}</span>
        {job.location ? <span className="muted small">{job.location}</span> : null}
        {job.is_pm_role ? (
          <span className="tag" title="The description frames this as a product/people management role.">
            PM
          </span>
        ) : null}
        {hint ? (
          <span
            className="muted small"
            title={
              hint === "desc not checked"
                ? "Nobody has read this description yet, so the experience and PM tests had nothing to judge. It passed every test that reads the title."
                : "The minimum years of experience the description states."
            }
          >
            {hint}
          </span>
        ) : null}
        {mode === "all" ? (
          <>
            <span className={job.status === "open" ? "tag on" : "tag off"}>{job.status}</span>
            <span className="muted small">{job.notified ? "notified" : "not notified"}</span>
          </>
        ) : null}
      </div>
    </li>
  );
}
