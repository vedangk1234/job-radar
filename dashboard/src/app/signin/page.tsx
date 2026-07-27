import { auth, isAuthorised, missingConfig } from "@/auth";
import { redirect } from "next/navigation";

import { signInWithGoogle } from "@/app/actions";

export const dynamic = "force-dynamic";

/**
 * The one page reachable while signed out.
 *
 * It is honest about the lock rather than coy about it: an account that is not
 * ALLOWED_EMAIL will authenticate with Google perfectly and still be refused,
 * and being told that up front beats an unexplained bounce back to this page.
 * There is nothing to leak — the address is already in the operator's own
 * OAuth consent screen.
 */
export default async function SignIn({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; error?: string }>;
}) {
  const session = await auth();
  const { next, error } = await searchParams;

  if (isAuthorised(session)) {
    redirect(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
  }

  const missing = missingConfig();

  return (
    <div className="signin">
      <div className="card">
        <h1>job-radar</h1>
        <p className="lede" style={{ marginBottom: 20 }}>
          One account has access. Google sign-in proves who you are; the dashboard then checks
          that the answer is the address in <code>ALLOWED_EMAIL</code>.
        </p>

        {missing.length > 0 ? (
          <p className="msg bad">
            Not configured: {missing.join(", ")} {missing.length === 1 ? "is" : "are"} missing.
            Copy <code>dashboard/.env.local.example</code> to <code>dashboard/.env.local</code>{" "}
            and fill it in, then restart <code>npm run dev</code>.
          </p>
        ) : (
          <form action={signInWithGoogle}>
            <input type="hidden" name="next" value={next ?? "/"} />
            <button className="primary" type="submit" style={{ width: "100%" }}>
              Continue with Google
            </button>
          </form>
        )}

        {error === "AccessDenied" ? (
          <p className="msg bad">
            That Google account signed in successfully and is still not allowed here. This
            dashboard is locked to a single address.
          </p>
        ) : error ? (
          <p className="msg bad">Sign-in failed ({error}).</p>
        ) : null}
      </div>
    </div>
  );
}
