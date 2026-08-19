"use client";

import { badgeClass, homeBadge, jobsBadge } from "@/lib/badges";
import type { JobView } from "@/lib/jobs";
import { descriptionHint, salaryOf } from "@/lib/match";

/**
 * One job row, on either screen.
 *
 * Its own file rather than a helper inside CompanyList for one practical
 * reason: it is the piece whose OUTPUT is the thing worth checking. A badge that
 * quietly stops rendering, or an Applied checkbox that appears on the wrong
 * screen, is not something a type checker or a database query can catch — so
 * `npm run ui-check` renders this component over live rows and asserts what the
 * markup actually contains. It can only do that if the component can be
 * imported on its own.
 *
 * The title IS the apply link: it is what a person wants to click, and making
 * them hunt for a separate "apply" affordance next to it would be decoration
 * standing between them and the posting.
 *
 * `rel="noopener noreferrer"` on every one of them. `noopener` because a tab
 * opened with target=_blank can otherwise reach back through window.opener and
 * navigate this page somewhere else; `noreferrer` because which dashboard sent a
 * click is nobody's business but this dashboard's.
 *
 * THE BADGE
 * ---------
 * At most one per row, and which one depends on which question the screen is
 * asking. Home shows NEW on the matches still queued for my inbox and nothing on
 * the ones already handled. Jobs shows SUGGESTED on the rows that match and
 * nothing on the rest — nothing there is filtered, so the useful fact is the
 * opposite one. Both are derived at render from data the row already carries; see
 * lib/badges.ts for why neither is a column, why the per-user half arrived in
 * Phase 1f, and why the NOTIFIED badge that used to sit beside NEW was removed.
 *
 * THE APPLIED CHECKBOX, AND WHY IT IS ONLY ON HOME
 * ------------------------------------------------
 * Jobs is the debugging view — "did the crawler miss it, or did the filter drop
 * it?" — and it lists the sales roles and the VP openings on purpose. A tick box
 * on all several hundred of those would be an invitation to curate a list that
 * exists to be raw. Home is the list of things worth applying to, so that is
 * where saying "I applied" belongs.
 *
 * An applied row is dimmed and struck through but NOT disabled: the link still
 * opens the posting. Having applied to a job is the most likely reason to want
 * to look at it again.
 */

export type Mode = "matches" | "all";

export function JobLine({
  job,
  mode,
  onApplied,
}: {
  job: JobView;
  mode: Mode;
  onApplied: (job: JobView, applied: boolean) => void;
}) {
  const hint = descriptionHint(job);
  const badge = mode === "matches" ? homeBadge(job) : jobsBadge(job);
  const pay = salaryLabel(job);

  return (
    <li className={`job${job.applied ? " applied" : ""}`}>
      <div className="job-line">
        {mode === "matches" ? (
          <label className="applied-box" title={appliedTitle(job)}>
            <input
              type="checkbox"
              checked={job.applied}
              onChange={(e) => onApplied(job, e.target.checked)}
            />
            <span className="sr-only">Applied to {job.title}</span>
            <span className="applied-word muted small" aria-hidden="true">
              Applied
            </span>
          </label>
        ) : null}
        <a className="job-title" href={job.url} target="_blank" rel="noopener noreferrer">
          {job.title}
        </a>
        {badge ? (
          <span className={badgeClass(badge.label)} title={badge.title}>
            {badge.label}
          </span>
        ) : null}
      </div>
      <div className="job-meta">
        <span className="tag">{job.city ?? "—"}</span>
        {job.location ? <span className="muted small">{job.location}</span> : null}
        {/*
          Pay, when the posting states any. See salaryLabel below for why "states
          any" is read off the two numbers rather than off `salary_checked`.
        */}
        {pay ? (
          <span className="tag pay" title="The pay this posting states, normalised to LPA.">
            {pay}
          </span>
        ) : null}
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
            {/*
              A `notified` / `not notified` word sat here and said the same thing
              the NOTIFIED badge said, so it went for the same reason: a
              user_job_notifications row means "handled", which since the
              filter-first slice is far more often a filter save recording the back
              catalogue than an email that was sent. Printing "notified" beside a
              job nobody was ever emailed about is the lie, and this screen is the
              debugging view — the one place a wrong fact is most likely to be
              believed. What a person needs here is `status` and SUGGESTED, both of
              which are still on the row.
            */}
          </>
        ) : null}
      </div>
    </li>
  );
}

/**
 * The pay this posting states, in LPA, or null when it states none.
 *
 * READ OFF THE TWO NUMBERS RATHER THAN OFF `salary_checked`, and the distinction
 * is worth one paragraph because the column exists precisely to be that
 * distinction. `salary_checked` means "a parser has read this posting's prose
 * looking for pay" — it separates "read it, states no pay" from "nobody has
 * looked", which is a fact about our COVERAGE and the reason the column is not a
 * second `jobs.notified`. What a row on a screen shows is a fact about the JOB,
 * and both of those states print the same thing here: nothing. So this needs one
 * bit of information the row already carries, and taking `salary_checked` as
 * well would mean widening jobColumns() — which would put a lifecycle column into
 * lib/jobs.setApplied's RETURNING, in the one statement whose whole claim is that
 * it names two columns and reads back two columns.
 *
 * lib/match.salaryOf is the reader, which is also what resolves the NUMERIC
 * -comes-back-as-a-string divergence in one place rather than here.
 *
 * IT WILL RENDER ON ALMOST NOTHING TODAY, and that is the data rather than the
 * code: `salary_min` and `salary_max` are NULL on every row of the live board,
 * because the parser refuses on every real description it has been shown. An
 * unpriced job is shown to everyone regardless — the salary filter is the one
 * that fails OPEN — so a job with no pay tag is not a job that failed a test.
 */
function salaryLabel(job: JobView): string | null {
  const [low, high] = salaryOf(job);
  const lpa = (value: number) => `₹${Number.isInteger(value) ? value : value.toFixed(1)}L`;
  if (low !== null && high !== null) return `${lpa(low)} – ${lpa(high)}`;
  if (low !== null) return `${lpa(low)}+`;
  if (high !== null) return `up to ${lpa(high)}`;
  return null;
}

/**
 * The checkbox tooltip. It names the date when there is one, because
 * `applied_at` is otherwise invisible — the row shows a tick, and "when" is the
 * question a tick immediately raises.
 *
 * Formatted in the browser's own locale rather than as the stored UTC string:
 * the timestamp is the database clock's, but the person reading it is here.
 */
function appliedTitle(job: JobView): string {
  if (!job.applied) return "Tick this once you have applied. It will dim and sink to the bottom.";
  if (!job.applied_at) return "Marked as applied.";
  return `Applied on ${new Date(job.applied_at).toLocaleString()}. Untick to undo.`;
}
