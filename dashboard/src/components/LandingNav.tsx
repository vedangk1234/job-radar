"use client";

import { useEffect, useState } from "react";

/**
 * The landing page's nav bar, and the only client component on the page.
 *
 * It exists for one cosmetic fact the server cannot know: whether you have
 * scrolled. Above 40px the bar is transparent over the photograph; past it, dark
 * glass with a hairline under it.
 *
 * NOTHING LOAD-BEARING LIVES HERE. The sign-in button inside it is a plain GET
 * form to /auth/signin, rendered by the server and submitted by the browser, so
 * with JavaScript off or not yet hydrated the bar simply stays transparent and
 * the button still works. That is the same argument the page itself is built on
 * — see the note in signin/page.tsx — and it is why the scroll listener was
 * allowed to be a client component at all.
 */
export function LandingNav({ children }: { children: React.ReactNode }) {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    // Read once on mount as well as on scroll: a reload halfway down the page
    // restores the scroll position without ever firing a scroll event, and the
    // bar would be transparent over black until you touched the wheel.
    const onScroll = () => setScrolled(window.scrollY > 40);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return <nav className={`ss-nav${scrolled ? " is-scrolled" : ""}`}>{children}</nav>;
}
