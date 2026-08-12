import Link from "next/link";

import { byMatchesThenName, listCompanyCards } from "@/lib/companies";
import { requirePage } from "@/lib/guard";
import { CompanyList } from "@/components/CompanyList";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Home — "what fits me right now", by company.
 *
 * The "For you" count is not a dashboard opinion about a job. It is
 * notify/match.is_match, ported to lib/match.ts and parity-checked against the
 * Python over the live table by `npm run match-parity`. A job counted here is a
 * job the digest would email; if the two ever disagree, that check fails before
 * anyone has to notice a number looked wrong.
 *
 * One thing is deliberately NOT part of the filter: whether a digest has already
 * handled it. A job that went out this morning still fits, so it still shows.
 * Home is not the outbox. It is however what the NEW badge reports — the same
 * fact, shown rather than filtered on, so "is this still queued for me" is
 * answerable at a glance without hiding anything. Since Phase 1f that fact is
 * per-user: a row in user_job_notifications for the account the session resolves
 * to, never a boolean on the job that another person's digest could have flipped.
 * Since Phase 2a that account is the one in your SESSION rather than the one
 * MY_EMAIL names.
 *
 * Ticking "Applied" on a row is the one write either screen makes to the jobs
 * table. See lib/jobs.setApplied for why those two columns are the dashboard's
 * while the rest of the table is the crawler's.
 *
 * IT IS THE WHOLE CATALOG, AND IT WAS YOUR WATCH LIST FOR ONE SLICE IN BETWEEN.
 * The picker slice narrowed this page to `user_companies` on a sound argument: the
 * digest queue opened with `EXISTS (user_companies …)`, so a "for you" count over
 * a company you did not watch counted jobs that could never arrive by email. The
 * filter-first slice removed that clause — company tracking is unlimited and free,
 * so a match anywhere in the catalog can reach any inbox — and the argument went
 * with it. The count was always computed over the catalog anyway; only the list of
 * companies beside it was narrowed, so this is the page agreeing with its own
 * numbers again. /watchlist is still where you pin companies.
 *
 * Rendered on the server — the page is already behind the session gate, so
 * reading the database directly beats making the browser round-trip through an
 * API route that would re-check the same session.
 */
export default async function HomePage() {
  const { userId } = await requirePage();

  const companies = (await listCompanyCards(userId)).sort(byMatchesThenName);
  const matches = companies.reduce((n, c) => n + c.match_jobs, 0);
  const withMatches = companies.filter((c) => c.match_jobs > 0).length;
  const openJobs = companies.reduce((n, c) => n + c.open_jobs, 0);

  return (
    <>
      <h1>Home</h1>
      <p className="lede">
        <strong>{matches}</strong> of {openJobs} open jobs fit your preferences, across{" "}
        {withMatches} of the {companies.length} compan
        {companies.length === 1 ? "y" : "ies"} we track. Same filter as the digest email — city,
        keyword, no excluded word, no level above your ceiling, and nothing the description
        marked as a PM role or a 3+-years role. Your filter decides this, not a company list:
        every board we crawl is matched against it. Expand a company to see its matches; the
        title is the apply link, and the checkbox beside it records that you took it.
      </p>
      <CompanyList
        initial={companies}
        mode="matches"
        empty={
          <>
            The catalog is empty, so there is nothing to match against yet.{" "}
            <Link href="/add">Add a company board</Link> and the hourly crawl will start
            storing its jobs.
          </>
        }
      />
      <p className="muted small" style={{ marginTop: 14 }}>
        A <span className="badge new">new</span> badge marks a match that is still queued for
        your inbox; a match with no badge has already been handled, either because a digest
        sent it or because it was already on the board when you set your filter up. It is
        computed from the row as it stands — nothing is stored, so editing your preferences
        re-decides every badge on the next load. Ticking{" "}
        <strong>Applied</strong> dims the job, strikes the title through and sinks it to the
        bottom of its company; the link still works, and the crawler never clears the tick.
      </p>
      <p className="muted small" style={{ marginTop: 10 }}>
        A job already sent in a digest still appears here — this is what fits, not what is
        unsent. Descriptions are read during the crawl, so a matching job&rsquo;s experience
        requirement is known within one crawl of it appearing.{" "}
        <code>desc not checked</code> marks the few minutes before that happens, and{" "}
        <code>desc unavailable</code> marks a board that would not give us the description at
        all — both mean the job passed on its title alone and could still turn out to want six
        years.
      </p>
    </>
  );
}
