import { redirect } from "next/navigation";

import { getAuthUser } from "@/lib/supabase/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Which required environment variables are absent, for the message below. */
function missingConfig(): string[] {
  const missing: string[] = [];
  if (!(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "").trim()) missing.push("NEXT_PUBLIC_SUPABASE_URL");
  if (!(process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "").trim()) {
    missing.push("NEXT_PUBLIC_SUPABASE_ANON_KEY");
  }
  return missing;
}

const MESSAGES: Record<string, string> = {
  email_unverified:
    "Google signed you in but did not confirm your email address as verified. " +
    "An unverified address can belong to someone else, so it cannot be used to " +
    "open or claim an account here.",
  no_code: "Google sent you back without an authorisation code. Try again.",
  access_denied: "You cancelled at Google's consent screen, so no account was created.",
};

/**
 * The one page reachable while signed out.
 *
 * PHASE 2a REWROTE WHAT THIS PAGE SAYS, because what it said stopped being
 * true. It used to explain that Google proves who you are and the dashboard
 * then checks the answer is ALLOWED_EMAIL — honest about a lock that genuinely
 * existed. There is no lock now: any Google account with a verified address is a
 * real account here, and this is a sign-UP page as much as a sign-in one. Saying
 * so plainly matters more than it looks, because the two flows are one button
 * and a person has no way to tell which one they are about to take.
 *
 * THE BUTTON IS A PLAIN GET FORM to a Route Handler, not a Server Action, and
 * that is a fix rather than a preference. The action minted the PKCE verifier
 * correctly and still could not sign anybody in: redirect() from a Server Action
 * is a client-router navigation, and for an external URL it bounced straight
 * back to this page without ever reaching Google — POST /signin 303, GET /signin
 * 200, and no row in auth.flow_state to show for it. A GET form needs no
 * JavaScript either, which was the property the action was chosen for, so
 * nothing was given up. See app/auth/signin/route.ts.
 */
export default async function SignIn({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; error?: string }>;
}) {
  const user = await getAuthUser();
  const { next, error } = await searchParams;

  if (user) {
    redirect(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
  }

  const missing = missingConfig();

  return (
    <div className="signin">
      <div className="card">
        <h1>job-radar</h1>
        <p className="lede" style={{ marginBottom: 20 }}>
          Sign in with Google. If you have not been here before this creates your account —
          your own filter, your own watch list, your own digest.
        </p>

        {missing.length > 0 ? (
          <p className="msg bad">
            Not configured: {missing.join(", ")} {missing.length === 1 ? "is" : "are"} missing.
            Copy <code>dashboard/.env.local.example</code> to <code>dashboard/.env.local</code>{" "}
            and fill it in, then restart <code>npm run dev</code>.
          </p>
        ) : (
          <form action="/auth/signin" method="GET">
            <input type="hidden" name="next" value={next ?? "/"} />
            <button className="primary" type="submit" style={{ width: "100%" }}>
              Continue with Google
            </button>
          </form>
        )}

        {error ? (
          <p className="msg bad">{MESSAGES[error] ?? `Sign-in failed (${error}).`}</p>
        ) : null}

        <p className="muted small" style={{ marginTop: 18 }}>
          We use your Google address to identify your account and to send your digest. Nothing
          else is read from your Google account.
        </p>
      </div>
    </div>
  );
}
