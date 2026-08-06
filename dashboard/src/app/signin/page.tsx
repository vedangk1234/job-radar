import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { LandingNav } from "@/components/LandingNav";
import { display, mono } from "@/fonts";
import { getAuthUser } from "@/lib/supabase/server";

import "../landing.css";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "ShowStencil — hear about the job before everyone else",
  description:
    "ShowStencil watches companies' own careers pages in Bangalore, Hyderabad and Pune every hour, and emails you new engineering roles the hour they post — before they reach LinkedIn or Naukri. Ad-free.",
};

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

/** Google's own mark, inline. A page that self-hosts its fonts to avoid a
 *  third-party request should not then fetch an 18px logo from a CDN. */
function GoogleMark() {
  return (
    <svg viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path
        fill="#4285F4"
        d="M45.12 24.5c0-1.56-.14-3.06-.4-4.5H24v8.51h11.84c-.51 2.75-2.06 5.08-4.39 6.64v5.52h7.11c4.16-3.83 6.56-9.47 6.56-16.17z"
      />
      <path
        fill="#34A853"
        d="M24 46c5.94 0 10.92-1.97 14.56-5.33l-7.11-5.52c-1.97 1.32-4.49 2.1-7.45 2.1-5.73 0-10.58-3.87-12.31-9.07H4.34v5.7A21.99 21.99 0 0 0 24 46z"
      />
      <path
        fill="#FBBC05"
        d="M11.69 28.18A13.2 13.2 0 0 1 11 24c0-1.45.25-2.86.69-4.18v-5.7H4.34A21.99 21.99 0 0 0 2 24c0 3.55.85 6.91 2.34 9.88l7.35-5.7z"
      />
      <path
        fill="#EA4335"
        d="M24 10.75c3.23 0 6.13 1.11 8.41 3.29l6.31-6.31C34.91 4.18 29.93 2 24 2 15.4 2 7.96 6.93 4.34 14.12l7.35 5.7c1.73-5.2 6.58-9.07 12.31-9.07z"
      />
    </svg>
  );
}

/**
 * EVERY CALL TO ACTION ON THIS PAGE IS ONE OF THESE, and that is the whole of
 * the auth wiring: a plain GET form to a Route Handler — see
 * app/auth/signin/route.ts for why it cannot be a Server Action — which needs no
 * JavaScript and works before hydration, on the slow phone this page is written
 * for. Six of them render: the nav pill, the hero button, the three pricing
 * buttons and the card at the bottom.
 *
 * The design draws them as anchors to #signin. An anchor cannot start an OAuth
 * flow, and a button that scrolls you to another button is a page asking to be
 * clicked twice, so they are submits wearing the same pill.
 */
function SignIn({
  next,
  label,
  className,
  mark = false,
}: {
  next: string;
  label: string;
  className: string;
  mark?: boolean;
}) {
  return (
    <form className="ss-form" action="/auth/signin" method="GET">
      <input type="hidden" name="next" value={next} />
      <button className={className} type="submit">
        {mark ? <GoogleMark /> : null}
        {label}
      </button>
    </form>
  );
}

/** The same pill, when there is no Supabase to sign in to. It scrolls to the
 *  sign-in block, which is where the explanation is. Local-dev only: on Vercel
 *  the two variables are always set. */
function SignInPlaceholder({ label, className }: { label: string; className: string }) {
  return (
    <a className={className} href="#signin">
      {label}
    </a>
  );
}

/** The wordmark. "Stencil" is cut out of the mark rather than written on it —
 *  see landing.css for what happens to it once the nav goes opaque. */
function Wordmark() {
  return (
    <>
      Show<span className="ss-brand-cut">Stencil</span>
      <span>.</span>
    </>
  );
}

/**
 * THE LANDING PAGE, WHICH IS ALSO THE SIGN-IN PAGE.
 *
 * A full public marketing page whose every call to action is the one action:
 * sign in with Google, which is also sign up with Google. Any Google account
 * with a verified address is a real account here, and a person has no way to
 * tell which of the two they are about to do — so the copy under the button says
 * both, rather than leaving it to be discovered.
 *
 * IT IS A SERVER COMPONENT ON THE NODE RUNTIME, and it stays one. The only
 * client code is components/LandingNav.tsx, which knows whether you have
 * scrolled and nothing else.
 *
 * WHAT THE PRICING SECTION IS AND IS NOT. There are three tiers with real
 * numbers on them and no billing behind any of them. Rather than delete the
 * section or ship a "Start free trial" button that starts nothing, the two paid
 * CTAs say what they are — join the waitlist, coming soon — sign you in exactly
 * like the free one, and a line under the heading says the paid tiers are not
 * live. A price is a promise; an unimplemented checkout behind it is the one
 * kind of dishonesty a page like this cannot afford.
 */
export default async function Landing({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; error?: string; deleted?: string }>;
}) {
  const user = await getAuthUser();
  const { next, error, deleted } = await searchParams;

  if (user) {
    redirect(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
  }

  const missing = missingConfig();
  const configured = missing.length === 0;
  const target = next ?? "/";

  const configNotice = (
    <p className="ss-msg ss-msg-bad">
      Not configured: {missing.join(", ")} {missing.length === 1 ? "is" : "are"} missing. Copy{" "}
      <code>dashboard/.env.local.example</code> to <code>dashboard/.env.local</code> and fill it in,
      then restart <code>npm run dev</code>.
    </p>
  );

  /** One CTA, in whichever of the two states the page is in. */
  const cta = (label: string, className: string, mark = false) =>
    configured ? (
      <SignIn next={target} label={label} className={className} mark={mark} />
    ) : (
      <SignInPlaceholder label={label} className={className} />
    );

  return (
    <div className={`ss ${display.variable} ${mono.variable}`}>
      {/* 0 — the photograph, four black layers deep, behind everything ------ */}
      <div className="ss-backdrop" aria-hidden="true">
        <div className="ss-backdrop-img" />
        <div className="ss-backdrop-scrim" />
        <div className="ss-backdrop-radial" />
        <div className="ss-backdrop-vertical" />
        <div className="ss-backdrop-diagonal" />
      </div>
      <div className="ss-grain" aria-hidden="true" />

      {/* 1 — nav ---------------------------------------------------------- */}
      <LandingNav>
        <div className="ss-nav-inner">
          <a className="ss-brand" href="#top">
            <Wordmark />
          </a>
          <div className="ss-nav-links">
            <a className="ss-nav-link" href="#how">
              How it works
            </a>
            <a className="ss-nav-link" href="#features">
              Features
            </a>
            <a className="ss-nav-link" href="#pricing">
              Pricing
            </a>
            {cta("Sign in", "ss-btn ss-btn-solid ss-btn-nav")}
          </div>
        </div>
      </LandingNav>

      <div className="ss-content">
        {/* 2 — hero ------------------------------------------------------- */}
        <header className="ss-hero" id="top">
          <p className="ss-eyebrow ss-rise">
            <span className="ss-dot" />
            <span className="ss-eyebrow-text">Job alerts, straight from the source</span>
          </p>

          <h1 className="ss-hero-title ss-rise ss-rise-2">
            <Wordmark />
          </h1>

          <p className="ss-hero-lead ss-rise ss-rise-3">
            Hear about the job before everyone else.
          </p>

          <p className="ss-hero-sub ss-rise ss-rise-4">
            ShowStencil watches companies&rsquo; own careers pages in Bangalore, Hyderabad and Pune,
            and emails new engineering roles the hour they post — so you apply first, not late
            alongside everyone else.
          </p>

          {/*
            * The three messages, all of which arrive on the query string and all
            * of which belong here rather than in the sign-in block five sections
            * down: a person sent back by a failed sign-in, or by deleting their
            * account, lands at the top of the page and reads downward.
            */}
          {error ? (
            <p className="ss-msg ss-msg-bad">{MESSAGES[error] ?? `Sign-in failed (${error}).`}</p>
          ) : null}

          {/*
            * The one page a just-deleted account can still be shown, because it
            * is the one page that does not require a session. Saying it plainly
            * here is the only confirmation erasure can ever give: there is no
            * account left to send a receipt to, and no inbox this app is still
            * allowed to write to. Silence would be indistinguishable from having
            * been signed out by a timeout.
            */}
          {deleted ? (
            <p className="ss-msg ss-msg-ok">
              Your account and everything attached to it have been deleted — your filter, your watch
              list, your notification history and your sign-in identity. Nothing of yours is left
              here. Signing in again would start a completely new account.
            </p>
          ) : null}

          {configured ? null : configNotice}

          <div className="ss-hero-cta ss-rise ss-rise-5">
            {cta("Start free with Google", "ss-btn ss-btn-solid ss-btn-hero")}
            <a className="ss-btn ss-btn-ghost ss-btn-hero" href="#how">
              See how it works
            </a>
          </div>
        </header>

        {/* the fade from photograph to solid black */}
        <div className="ss-veil" aria-hidden="true" />

        <div className="ss-solid">
          {/* 3 — manifesto ------------------------------------------------ */}
          <section className="ss-manifesto">
            <p>
              Speed and signal. You hear about relevant roles first,{" "}
              <span className="ss-dim">
                straight from the company — not a job board — filtered down to what actually fits
                your experience and goals.
              </span>{" "}
              Ad-free and privacy-first: you&rsquo;re the user, not the product.
            </p>
          </section>

          {/* 4 — how it works --------------------------------------------- */}
          <section className="ss-section ss-how" id="how">
            <p className="ss-eyebrow">
              <span className="ss-dot" />
              <span className="ss-eyebrow-text">How it works</span>
            </p>

            <div className="ss-steps">
              <div className="ss-step">
                <div className="ss-step-num">01</div>
                <h3>Set up your profile</h3>
                <p>
                  Tell us your experience level, the kind of roles you want, and the companies
                  you&rsquo;d like to watch.
                </p>
              </div>
              <div className="ss-step">
                <div className="ss-step-num">02</div>
                <h3>We watch, every hour</h3>
                <p>
                  Every hour ShowStencil checks each company&rsquo;s careers page directly for
                  brand-new postings.
                </p>
              </div>
              <div className="ss-step">
                <div className="ss-step-num">03</div>
                <h3>Get the digest</h3>
                <p>
                  When a role fits your curation, it lands in your inbox — genuinely new, once each.
                  Nothing repeated, nothing missed.
                </p>
              </div>
            </div>
          </section>

          {/* 5 — features -------------------------------------------------- */}
          <section className="ss-section ss-features" id="features">
            <p className="ss-eyebrow">
              <span className="ss-dot" />
              <span className="ss-eyebrow-text">Features</span>
            </p>
            <h2 className="ss-section-title">Built for engineers who want to hear first.</h2>

            <div className="ss-cards">
              <div className="ss-card">
                <h3>Straight from the source</h3>
                <p>
                  We read companies&rsquo; own careers pages — not LinkedIn or Naukri. You see it
                  before the boards do.
                </p>
              </div>
              <div className="ss-card">
                <h3>Curated to your level</h3>
                <p>
                  Tell us your experience and goals; we filter to roles that actually fit — junior to
                  staff.
                </p>
              </div>
              <div className="ss-card">
                <h3>Checked every hour</h3>
                <p>Fresh crawls around the clock, so a new posting reaches you in minutes, not days.</p>
              </div>
              <div className="ss-card">
                <h3>New once, never twice</h3>
                <p>
                  It remembers everything it has shown you. Only genuinely new roles, once each.
                </p>
              </div>
              <div className="ss-card">
                <h3>Your private dashboard</h3>
                <p>
                  Manage the companies you watch, tune filters, and review every match in one place.
                </p>
              </div>
              <div className="ss-card">
                <h3>Ad-free &amp; privacy-first</h3>
                <p>No ads, no reselling your data. You&rsquo;re the user here, not the product.</p>
              </div>
            </div>
          </section>

          {/* 6 — pricing --------------------------------------------------- */}
          <section className="ss-section ss-pricing" id="pricing">
            <p className="ss-eyebrow">
              <span className="ss-dot" />
              <span className="ss-eyebrow-text">Pricing</span>
            </p>
            <h2 className="ss-section-title">Start free. Upgrade when you&rsquo;re serious.</h2>
            <p className="ss-pricing-note">
              Pro isn&rsquo;t live yet — there is no card to enter and nothing to cancel. Free is the
              whole product today; the paid buttons below sign you in and put you on the list for
              when the rest of it ships.
            </p>

            <div className="ss-tiers">
              <div className="ss-tier">
                <div className="ss-tier-name">Free</div>
                <div className="ss-tier-price">₹0</div>
                <div className="ss-tier-for">for every job seeker, forever</div>
                <ul>
                  <li>Watch up to 5 companies</li>
                  <li>Hourly checks</li>
                  <li>Bangalore, Hyderabad &amp; Pune</li>
                  <li>Daily digest email</li>
                </ul>
                {cta("Get started", "ss-btn ss-btn-ghost ss-btn-block")}
              </div>

              <div className="ss-tier ss-tier-featured">
                <div className="ss-tier-flag">Popular</div>
                <div className="ss-tier-name">Pro</div>
                <div className="ss-tier-price">
                  ₹299<span>/mo</span>
                </div>
                <div className="ss-tier-for">for an active job search</div>
                <ul>
                  <li>Unlimited companies</li>
                  <li>As-posted alerts, not just digests</li>
                  <li>Advanced role &amp; level filters</li>
                  <li>Applied-tracking dashboard</li>
                </ul>
                {cta("Join the waitlist", "ss-btn ss-btn-solid ss-btn-block")}
              </div>

              <div className="ss-tier">
                <div className="ss-tier-name">Pro annual</div>
                <div className="ss-tier-price">
                  ₹2499<span>/yr</span>
                </div>
                <div className="ss-tier-for">everything in Pro, two months free</div>
                <ul>
                  <li>All Pro features</li>
                  <li>Best value — save 30%</li>
                  <li>Priority crawl queue</li>
                  <li>Early access to new sources</li>
                </ul>
                {cta("Coming soon", "ss-btn ss-btn-ghost ss-btn-block")}
              </div>
            </div>
          </section>

          {/* 7 — sign in ---------------------------------------------------- */}
          <section className="ss-section ss-signin" id="signin">
            <div>
              <p className="ss-eyebrow">
                <span className="ss-dot" />
                <span className="ss-eyebrow-text">Get in</span>
              </p>
              <h2 className="ss-signin-title">
                Start hearing
                <br />
                about jobs first.
              </h2>
              <p className="ss-signin-blurb">
                Sign in with Google and set up your first watch list in under a minute. No card, no
                resume upload, no spam.
              </p>
            </div>

            <div className="ss-signin-card">
              <div className="ss-signin-card-head">Sign in / Create account</div>

              {configured ? (
                <>
                  {cta("Continue with Google", "ss-btn ss-btn-solid ss-btn-google", true)}
                  <div className="ss-signin-marks">
                    <span>✓ Free to start</span>
                    <span>✓ Delete anytime</span>
                  </div>
                  <p className="ss-signin-fine">
                    One click signs you in or creates your account. We only ever see your email — no
                    passwords, no ads.
                  </p>
                </>
              ) : (
                configNotice
              )}
            </div>
          </section>

          {/* 8 — footer ----------------------------------------------------- */}
          <footer className="ss-footer">
            <a className="ss-brand" href="#top">
              <Wordmark />
            </a>
            <div className="ss-footer-links">
              <a href="#how">How it works</a>
              <a href="#features">Features</a>
              <a href="#pricing">Pricing</a>
              <a href="#signin">Sign in</a>
            </div>
            <span className="ss-footer-copy">© 2026 ShowStencil</span>
          </footer>
        </div>
      </div>
    </div>
  );
}
