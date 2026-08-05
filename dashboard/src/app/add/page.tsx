import { AddCompany } from "@/components/AddCompany";
import { requirePage } from "@/lib/guard";

export const dynamic = "force-dynamic";

export default async function AddPage() {
  // Guarded since Phase 2a. This page reads nothing, so the omission leaked no
  // data — but it left "every page checks for itself" untrue, and a rule with
  // one silent exception is a rule nobody can rely on when they add the next
  // page. The writes behind it were always guarded at their routes.
  await requirePage();

  return (
    <>
      <h1>Add a company</h1>
      <p className="lede">
        Two ways in. By name guesses the board token the way{" "}
        <code>scripts/seed_companies.py</code> does; by URL takes apart a board or Workday
        careers page you are already looking at. Both check the board answers before offering to
        store it, and both upsert on (ats, token) — adding the same company twice refreshes one
        row rather than making two.
      </p>
      <AddCompany />
    </>
  );
}
