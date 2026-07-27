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
 * `notified` is shown here and nowhere else. It was only ever dangerous as an
 * editable field — the crawler owns it, and a dashboard that could flip it could
 * silently re-send or un-send a digest. Reading it is just a fact about the row.
 */
export default async function JobsPage() {
  await requirePage();

  const companies = await listCompanyCards();
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
        Counts are open jobs only; hover a count to see how many closed postings are also
        stored. For the filtered view — what actually fits you — see Home.
      </p>
    </>
  );
}
