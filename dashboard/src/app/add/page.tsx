import { AddCompany } from "@/components/AddCompany";

export const dynamic = "force-dynamic";

export default function AddPage() {
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
