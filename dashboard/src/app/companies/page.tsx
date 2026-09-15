import { listCompanyCards, trackedCount } from "@/lib/companies";
import { requirePage } from "@/lib/guard";
import { TRACKED_CAP } from "@/lib/labels";
import { CompanyManager } from "@/components/CompanyManager";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Companies — choose which boards the hourly crawl fetches.
 *
 * This is the management surface for `companies.tracked`, the crawler's
 * catalog-wide flag (scripts/crawl.py reads `WHERE tracked = TRUE`). Turning a
 * company on adds it to the hourly crawl; turning it off freezes it — its stored
 * jobs stay browsable, nothing new arrives. Capped at TRACKED_CAP because the cron
 * has a fixed time budget; the cap is enforced server-side in setTracked.
 *
 * It is the SHARED catalog, not a per-account list — one selection for the whole
 * install. The per-user "pin companies I care about" concept is /watchlist, a
 * different table. Rendered on the server behind the session gate; the manager is
 * handed every company with its match counts so sorting by relevance needs no
 * round trip.
 */
export default async function CompaniesPage() {
  const { userId } = await requirePage();

  const [cards, tracked] = await Promise.all([listCompanyCards(userId), trackedCount()]);

  return (
    <>
      <h1>Companies</h1>
      <p className="lede">
        <strong>{tracked}</strong> of {cards.length} companies are being crawled, up to a limit
        of {TRACKED_CAP}. Crawled companies are fetched every hour and appear on your Home and
        Jobs screens; the rest stay in the catalog — browsable, but not refreshed — until you add
        them here. Sort by <em>Most relevant</em> to find companies with jobs that fit you that
        you are not yet crawling.
      </p>
      <CompanyManager initial={cards} cap={TRACKED_CAP} trackedCount={tracked} />
      <p className="muted small" style={{ marginTop: 14 }}>
        This is the shared crawl list for the whole install, not a per-account setting. Untracking
        a company stops it being fetched for everyone; its already-stored jobs remain visible.
      </p>
    </>
  );
}
