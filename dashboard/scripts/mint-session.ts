/**
 * Mint a valid session cookie for LOCAL TESTING ONLY.
 *
 *     npx tsx scripts/mint-session.ts [email]
 *
 * Google's consent screen needs a browser, which makes the authorised half of
 * this dashboard untestable from a terminal — the half where every database
 * write lives. This closes that gap: it encrypts a session token with the same
 * AUTH_SECRET and the same `encode` from next-auth/jwt that the real callback
 * uses, so the cookie it prints is indistinguishable from one Google issued.
 *
 * It proves everything after the OAuth handshake: the session gate, the
 * ALLOWED_EMAIL check, and every route behind them. It does NOT prove the
 * handshake itself — that the Google client id, secret and redirect URI are
 * right — which still needs one real browser sign-in.
 *
 * This is not a backdoor. It requires AUTH_SECRET, and anyone holding
 * AUTH_SECRET can already forge any session; the security of the whole scheme
 * is that the secret stays in .env.local. Pass any address to check that a
 * non-allowed account is refused.
 */

import { config } from "dotenv";
import { encode } from "next-auth/jwt";

config({ path: ".env.local" });

const secret = (process.env.AUTH_SECRET ?? "").trim();
if (!secret) {
  console.error("AUTH_SECRET is not set in .env.local");
  process.exit(1);
}

const email = process.argv[2] || (process.env.ALLOWED_EMAIL ?? "").trim();
if (!email) {
  console.error("no email given and ALLOWED_EMAIL is not set");
  process.exit(1);
}

// Over plain http (dev) Auth.js names the cookie `authjs.session-token`, and
// that name is also the encryption salt — get it wrong and the server decrypts
// to nothing and treats you as signed out.
const COOKIE = "authjs.session-token";

// Wrapped rather than top-level: tsx transpiles to CJS on this Node version,
// where top-level await is a syntax error.
async function main() {
  const token = await encode({
    token: {
      email,
      name: "local test",
      sub: "local-test",
      // The dashboard's own flag, set to what the real jwt callback WOULD
      // compute — so this cannot hand you authorisation the server would not
      // have granted. isAuthorised re-checks the email regardless.
      authorised: email.toLowerCase() === (process.env.ALLOWED_EMAIL ?? "").trim().toLowerCase(),
    },
    secret,
    salt: COOKIE,
    maxAge: 60 * 30,
  });
  console.log(`${COOKIE}=${token}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
