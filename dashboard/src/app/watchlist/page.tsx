import { listCompanies } from "@/lib/companies";
import { requirePage } from "@/lib/guard";
import { watchlistState } from "@/lib/watchlist";
import { WatchlistPicker } from "@/components/WatchlistPicker";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Watchlist — the full catalog, and which of it is yours.
 *
 * WHY THIS IS ITS OWN SCREEN RATHER THAN A CONTROL ON HOME. Home is now scoped to
 * the companies you watch, so a "watch this" button there could only ever appear
 * on companies you already watch — the add half would have nowhere to live. The
 * two screens answer different questions and only one of them is about the shared
 * catalog: this one browses fifty companies you mostly do not want, Home ranks the
 * few you do.
 *
 * It is NOT behind an onboarding exemption. A person mid-setup is sent to
 * /onboarding, where the same picker is rendered inline — see that page. Nothing
 * here needs its own hole in the gate, which is the property lib/guard.requirePage
 * is built to keep: three enumerated exemptions and a test that fails on a fourth.
 *
 * Rendered on the server, and the picker is handed the catalog rather than
 * fetching it. Fifty rows of (id, name, ats, token, open_jobs) is a few kilobytes
 * — this is the one screen where shipping the whole list IS the feature, unlike
 * the job rows CompanyList deliberately leaves on the server until a row opens.
 */
export default async function WatchlistPage() {
  const { userId } = await requirePage();

  const [catalog, state] = await Promise.all([listCompanies(), watchlistState(userId)]);

  return (
    <>
      <h1>Watchlist</h1>
      <p className="lede">
        <strong>{state.companyIds.length}</strong> of {catalog.length} companies in the catalog
        are yours. Your digest and your Home screen are built from these and nothing else &mdash;
        the rest are crawled hourly all the same, for everybody, and simply do not reach you. Add
        one and its existing postings are recorded as already seen, so the first email you get
        about it is a genuinely new job.
      </p>
      <WatchlistPicker catalog={catalog} initialWatched={state.companyIds} cap={state.cap} />
      <p className="muted small" style={{ marginTop: 14 }}>
        Watching a company is a setting on <em>your account</em>. It does not start or stop the
        crawler for anybody &mdash; that is the <code>tracked</code> flag on the shared catalog
        row, which the dashboard may read and never write. Removing a company here leaves the
        jobs you were already told about alone; re-adding it later will not re-send anything you
        have already seen.
      </p>
    </>
  );
}
