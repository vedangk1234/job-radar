import { deleteAccountAction } from "@/app/actions";
import type { AccountFootprint } from "@/lib/account";

/**
 * The delete-account surface. A SERVER component, and the absence of
 * "use client" at the top is the whole design.
 *
 * Every other interactive piece of this dashboard is a client component because
 * it has state to hold — a checkbox mid-flight, a list being edited. This has
 * none: a disclosure, a text field and a submit. Written as a server component
 * it ships no JavaScript at all and still works completely with scripting off,
 * which is the same argument sign-out is a server action for and is the house
 * style since Phase 1.
 *
 * <details> IS DOING REAL WORK HERE. The dangerous button is not on the screen
 * until somebody opens the disclosure, and <details> is native HTML — the
 * open/closed state costs nothing and survives JavaScript being off, where a
 * useState toggle would leave the form permanently expanded. Two deliberate
 * actions before the destructive one, without a line of script.
 *
 * WHY IT ASKS YOU TO TYPE YOUR ADDRESS RATHER THAN CLICK "yes, I am sure". A
 * second button is a second click, and a person clicking through a flow makes
 * both. Typing the address is the smallest interaction that cannot be performed
 * absent-mindedly, and it is the one this app can check honestly, because the
 * server already knows the answer from the session and compares there — see
 * app/actions.ts. Nothing on this page is trusted for it.
 *
 * THE COUNTS ARE NOT DECORATION. "This cannot be undone" is a sentence nobody
 * reads; the number of companies they curated and the number of jobs they were
 * told about is one they do. Under the DPDP Act the point of the confirmation is
 * informed consent to erasure, and naming what goes is what makes it informed.
 */
export function DeleteAccount({
  email,
  footprint,
  notice,
}: {
  email: string;
  footprint: AccountFootprint;
  notice?: string;
}) {
  return (
    <section className="panel danger-zone">
      <h2>Delete account</h2>
      <p className="hint">
        This erases your account and everything attached to it: your filter, your{" "}
        <strong>{footprint.companies}</strong> tracked{" "}
        {footprint.companies === 1 ? "company" : "companies"}, your{" "}
        <strong>{footprint.notifications.toLocaleString()}</strong> notification{" "}
        {footprint.notifications === 1 ? "record" : "records"}, and your sign-in identity. It
        takes effect immediately and it cannot be undone — there is no restore and no grace
        period. The shared job catalogue is not personal data and stays; nothing in it names you.
      </p>

      {notice === "mismatch" ? (
        <p className="msg bad">
          That did not match your email address, so nothing was deleted. Type{" "}
          <span className="mono">{email}</span> exactly to confirm.
        </p>
      ) : null}

      <details className="danger-disclose">
        <summary>I want to delete my account</summary>

        <form action={deleteAccountAction}>
          <label className="field">
            <span>
              Type <span className="mono">{email}</span> to confirm
            </span>
            <input
              type="text"
              name="confirm"
              required
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              aria-label={`Type ${email} to confirm deletion`}
            />
          </label>
          <button className="danger" type="submit">
            Delete my account and all my data
          </button>
        </form>
      </details>
    </section>
  );
}
