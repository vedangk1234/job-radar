import { listCompanyCards } from "@/lib/companies";
import { requirePage } from "@/lib/guard";
import { CompanyList } from "@/components/CompanyList";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Jobs — the same list, unfiltered. The debugging view.
 *
 * Expanding a company here shows every open posting it has: the staff roles,
 * the sales roles, the ones in cities you never asked for. That is the point.
 * When Home shows nothing for a company, this is the screen that answers
 * whether the crawler found nothing or the filter dropped everything — and the
 * two are very different bugs.
 *
 * Ordered by name rather than by count. Home ranks; this one is a reference,
 * and a reference you look things up in should be where you left it.
 *
 * "notified" is shown here as text, and since Phase 1f it means "a digest has
 * told YOU about this" — a row in user_job_notifications — rather than the old
 * global boolean on the job. It is read and never written: this screen has no
 * control that could add or remove such a row, and the send path is the only
 * thing entitled to write one.
 *
 * The one badge this screen shows is SUGGESTED, on the rows that also pass the
 * filter. It is the same shared matcher Home uses, applied to a list that is not
 * filtered by it — which is what makes it worth showing here and pointless on
 * Home, where it would be on every row. There is no Applied checkbox here: this
 * screen is the raw reference, and the tick belongs on the list of jobs worth
 * applying to.
 */
export default async function JobsPage() {
  const { userId } = await requirePage();

  const companies = await listCompanyCards(userId);
  const openJobs = companies.reduce((n, c) => n + c.open_jobs, 0);

  return (
    <>
      <h1>Jobs</h1>
      <p className="lede">
        Every open job the crawler currently holds — <strong>{openJobs}</strong> across{" "}
        {companies.length} companies — with no filtering at all. All seniorities, all cities,
        sales and HR included. Read-only: the crawler owns this table.
      </p>
      <CompanyList initial={companies} mode="all" />
      <p className="muted small" style={{ marginTop: 14 }}>
        A <span className="badge suggested">suggested</span> badge marks the postings that also
        pass your filter — the same matcher Home uses, run over this unfiltered list. Everything
        without one is a job the filter would drop, which is exactly what this screen is for.
        Counts are open jobs only; hover a count to see how many closed postings are also
        stored.
      </p>
    </>
  );
}
