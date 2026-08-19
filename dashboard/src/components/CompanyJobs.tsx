"use client";

import { JobLine, type Mode } from "@/components/JobLine";
import { groupJobsByCity } from "@/lib/cards";
import type { JobView } from "@/lib/jobs";

/**
 * An OPEN card's job list, grouped by city.
 *
 * ALL THE CITIES, NOT THE THREE THE CHIP ROW HAD ROOM FOR. The chips are a
 * summary with a "+more" on the end; this is the thing "+more" is a summary OF,
 * so truncating here too would leave the extra roles reachable from nowhere.
 *
 * ORDER IS INHERITED, NOT RECOMPUTED, and that is the load-bearing part. The jobs
 * arrive already sorted by CompanyList's one comparator — applied last, and
 * within that the server's newest-first — and lib/cards.groupJobsByCity keeps
 * input order inside each group. So ticking Applied still moves the row
 * immediately, still moves it only within its own city, and there is still
 * exactly one place in this app that decides what order jobs are in.
 *
 * A PRESENTATIONAL COMPONENT ON PURPOSE: jobs in, one callback out, no fetch and
 * no state. That is what lets `npm run ui-check` render it over LIVE rows from
 * lib/jobs.matchingJobsForCompany and assert the city headings, the apply links
 * and the checkboxes — the expanded card is fetched in the browser, so its markup
 * exists nowhere in a server render and would otherwise be the one part of this
 * slice nothing could check without a browser.
 */
export function CompanyJobs({
  jobs,
  mode,
  onApplied,
}: {
  jobs: JobView[];
  mode: Mode;
  onApplied: (job: JobView, applied: boolean) => void;
}) {
  const groups = groupJobsByCity(jobs);

  return (
    <div className="card-jobs">
      {groups.map((group) => (
        <section key={group.label} className="city-group">
          <h3 className="city-head">
            <span className="city-label" title={group.name}>
              {group.label}
            </span>
            <span className="muted small">
              {group.jobs.length} role{group.jobs.length === 1 ? "" : "s"}
            </span>
          </h3>
          <ul className="jobs">
            {group.jobs.map((job) => (
              <JobLine key={job.canonical_id} job={job} mode={mode} onApplied={onApplied} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
