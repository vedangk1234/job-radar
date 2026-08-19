import { monogram, tintIndex } from "@/lib/cards";

/**
 * A company's mark on its card. A MONOGRAM, AND ONLY A MONOGRAM, THIS SLICE.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO: fetch anything. There is no <img>, no
 * favicon guess, no Clearbit, no logo column and no cache. Real logos are Slice
 * 3's whole subject, and a placeholder that quietly started making a network
 * request per card would be that slice arriving early, unmeasured, on the one
 * screen a person opens first — fifty third-party requests, fifty chances to
 * hang, and a company's identity leaking to whoever serves the image.
 *
 * WHAT SLICE 3 CHANGES AND WHAT IT DOES NOT. It replaces what is DRAWN inside
 * this component and keeps the props: a card passes a name and gets a mark. The
 * monogram stays afterwards as the fallback for every company whose logo fetch
 * found nothing, which it will for some — so this is not scaffolding to be
 * deleted, it is the floor.
 *
 * The letters come from lib/cards.monogram and the tint from lib/cards.tintIndex,
 * both pure functions of the name, so a card cannot change colour between two
 * loads of the same page and `npm run ui-check` can assert the mark without a
 * browser.
 *
 * `aria-hidden`, because the company's name is rendered in full immediately
 * beside it. A screen reader announcing "A. Adobe" is reading the decoration
 * twice.
 */
export function CompanyLogo({ name }: { name: string }) {
  return (
    <span className={`company-logo tint-${tintIndex(name)}`} aria-hidden="true" title={name}>
      {monogram(name)}
    </span>
  );
}
