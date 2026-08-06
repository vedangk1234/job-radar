import localFont from "next/font/local";

/*
 * The landing page's two typefaces, SELF-HOSTED rather than pulled from
 * fonts.googleapis.com, and that is a product decision as much as a performance
 * one. The page's own promise is that the reader is the user and not the
 * product; a page that says so while making every visitor's browser announce
 * itself to a Google font CDN before the headline paints is arguing with itself.
 * The .woff2 files in src/fonts/ are the latin subsets, committed, so the page
 * makes no third-party request at all and `next build` needs no network.
 *
 * THERE IS NO SELF-HOSTED BODY FACE, and that is not an omission. The design
 * sets body copy in the system sans stack ('Helvetica Neue', Helvetica, Arial),
 * so the only faces that have to arrive over the wire are the two doing work no
 * system font can do: the serif that carries every heading and the mono that
 * carries every eyebrow. 58 KB for both.
 *
 * Instrument Serif ships as two static files rather than one variable font — it
 * has a single weight, and the italic is a genuinely drawn cut, not a slant. The
 * hero's one italic line is the reason the second file exists.
 *
 * IBM Plex Mono is 400 ONLY. Google's stylesheet offers 500 as well and the
 * design never asks for it; shipping a weight nothing selects is 15 KB spent on
 * nothing.
 */

export const display = localFont({
  src: [
    { path: "./fonts/instrument-serif-latin.woff2", weight: "400", style: "normal" },
    { path: "./fonts/instrument-serif-italic-latin.woff2", weight: "400", style: "italic" },
  ],
  variable: "--ss-display",
  display: "swap",
  // The design's own fallback, so the swap lands on the face it was drawn
  // against rather than on whatever the platform calls "serif".
  fallback: ["Georgia", "Times New Roman", "serif"],
  adjustFontFallback: "Times New Roman",
});

export const mono = localFont({
  src: "./fonts/ibm-plex-mono-latin.woff2",
  variable: "--ss-mono",
  display: "swap",
  weight: "400",
  style: "normal",
  fallback: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
  // Every use is an eyebrow at 10-12px with .22-.28em of tracking, where a
  // metric-matched fallback would buy nothing and a wrong one costs a reflow.
  adjustFontFallback: false,
});
