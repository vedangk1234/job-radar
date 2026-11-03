import Link from "next/link";

import { GRID_CAP } from "@/lib/cards";
import { byMatchesThenName, listCompanyCards, trackedCount } from "@/lib/companies";
import { requirePage } from "@/lib/guard";
import { TRACKED_CAP } from "@/lib/labels";
import { CompanyList } from "@/components/CompanyList";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Home — "what fits me right now", by company, as a grid of cards.
 *
 * The relevant-roles count is not a dashboard opinion about a job. It is
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
 * THE GRID IS ALWAYS FULL, AND THAT IS WHY THERE IS NO EMPTY STATE. Companies
 * with at least one relevant role come first, and the remaining slots are the
 * catalog in alphabetical order, each stating `0 relevant roles` honestly. A
 * screen that went blank when a filter matched nothing would be answering "you
 * have nothing" with "there is nothing", which is a different and less true
 * sentence — the boards are still there and still being crawled.
 *
 * SELECTION IS RELEVANCE-THEN-NAME; ORDER IS THE DROPDOWN'S. The slice below is
 * taken from the byMatchesThenName ordering and never from the ordering the
 * reader chose, so switching to "Latest" cannot decide WHICH companies are on the
 * page — every zero-relevance company ties at null there, and a company with
 * matches would drop off the grid for having nothing new today. See
 * lib/cards.GRID_CAP.
 *
 * Rendered on the server — the page is already behind the session gate, so
 * reading the database directly beats making the browser round-trip through an
 * API route that would re-check the same session.
 */
export default async function HomePage() {
  const { userId } = await requirePage();

  const [cards, tracked] = await Promise.all([listCompanyCards(userId), trackedCount()]);
  const all = cards.sort(byMatchesThenName);
  const companies = all.slice(0, GRID_CAP);
  const matches = all.reduce((n, c) => n + c.match_jobs, 0);
  const withMatches = all.filter((c) => c.match_jobs > 0).length;
  const openJobs = all.reduce((n, c) => n + c.open_jobs, 0);

  return (
    <>
      <h1>Home</h1>
      <p className="lede">
        <strong>{matches}</strong> of {openJobs} open jobs fit your preferences, across{" "}
        {withMatches} of the {all.length} compan
        {all.length === 1 ? "y" : "ies"} we track. Same filter as the digest email — city,
        keyword, no excluded word, no level above your ceiling, and nothing the description
        marked as a PM role or a 3+-years role. Your filter decides this, not a company list:
        every board we crawl is matched against it. Every count on a card — the big number and
        the city chips — is inside the cities you picked. Open a card to see its roles grouped
        by city; the title is the apply link, and the checkbox beside it records that you took
        it.
      </p>
      <CompanyList
        initial={companies}
        mode="matches"
        variant="grid"
        cap={TRACKED_CAP}
        trackedCount={tracked}
        // One instant for the whole page, decided by the server. See the prop's
        // comment in CompanyList for the hydration bug the alternative is.
        now={new Date().toISOString()}
        /*
          PRELOAD IS DELIBERATELY OFF — it was an egress leak, not just a nicety.
          `preload` fired a /api/companies/:id/jobs fetch for every one of the
          fifty cards the instant the grid painted, so each Home render pulled the
          whole board's candidate rows a SECOND time (matchCountsByCompany already
          pulled them once for the counts) across fifty round trips — on every
          visit, refresh and router prefetch. On the live free-tier database that
          was the dominant source of the egress that got the project restricted.

          The lazy expand-fetch (CompanyList's default) fetches a card's jobs only
          when it is actually opened, which is one bounded request for the one or
          two cards a person clicks rather than fifty for cards they never touch.
          The cost is a brief spinner the first time each card opens, which is the
          correct trade: the counts — the whole point of the grid — still come with
          the page and need no fetch.
        */
        empty={
          <>
            The catalog is empty, so there is nothing to match against yet.{" "}
            <Link href="/add">Add a company board</Link> and the hourly crawl will start
            storing its jobs.
          </>
        }
      />
      {all.length > companies.length ? (
        <p className="muted small" style={{ marginTop: 14 }}>
          Showing {companies.length} of {all.length} companies — every company with something
          for you, then the rest of the catalog alphabetically.
        </p>
      ) : null}
      <p className="muted small" style={{ marginTop: 14 }}>
        Cards are ordered by whichever sort is chosen above; it opens on the newest first while
        you have a role no digest has sent you yet, and on the biggest count otherwise. A{" "}
        <span className="badge new">new</span> badge marks a match that is still queued for
        your inbox; a match with no badge has already been handled, either because a digest
        sent it or because it was already on the board when you set your filter up. It is
        computed from the row as it stands — nothing is stored, so editing your preferences
        re-decides every badge on the next load. Ticking{" "}
        <strong>Applied</strong> dims the job, strikes the title through and sinks it to the
        bottom of its city; the link still works, and the crawler never clears the tick.
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
