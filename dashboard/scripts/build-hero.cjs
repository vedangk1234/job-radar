/*
 * A ONE-OFF ASSET STEP, run by hand — deliberately not wired into package.json.
 *
 *     node scripts/build-hero.cjs
 *
 * It turns the 1.6 MB source PNG into the WebP widths and the one JPEG fallback
 * that public/hero/ ships, and those OUTPUTS ARE COMMITTED. The source PNG is
 * not: it lives at the repo root, untracked, so Vercel builds from a checkout
 * that has never seen it. If the generated files are ever missing from a commit
 * the deployed hero is a black rectangle — there is nothing in the build that
 * could regenerate them. Re-run this only when the artwork itself changes.
 *
 * It needs `sharp`, which is present transitively via next's image optimiser
 * rather than declared here — which is exactly why this is a manual step and not
 * a build hook. A build that depends on a transitive package is a build that
 * breaks on somebody else's `npm install`.
 *
 * WHAT THE IMAGE IS FOR, AND WHY IT IS NOT CROPPED THE WAY IT USED TO BE. The
 * previous landing page hung this picture in the page as an <img>, cropped its
 * blank top quarter away and continued the plaster in CSS. This design uses it
 * as a fixed, full-bleed, darkened COVER backdrop behind the whole page, so
 * there is no join to hide and no negative space to extend: `cover` decides what
 * survives at each viewport, and the browser does the cropping. Only the parts
 * that are not artwork come off here.
 *
 * TWO EDGES COME OFF, BOTH MEASURED RATHER THAN GUESSED (see the profiles the
 * script prints):
 *
 *   - the last 33 rows. Row luma sits around 168 through y=737 and jumps to 183
 *     at y=740 and stays there: a container edge that came with the file, not
 *     plaster. On a wide, short viewport `cover` shows the bottom of the source,
 *     and that band reads as a pale seam across the foot of the hero.
 *   - two columns from each side. The far right column drops to 125 against its
 *     neighbours' 129 — a single-pixel rule. The gentle falloff either side of it
 *     is the photograph's own vignette and is kept.
 *
 * WIDTHS. The source is 1365x768, so 1365 IS the ceiling — asking for 1600 or
 * 2400 would ship upscaled bytes carrying no more detail than the file has. Two
 * widths: 1080 for phones (a 390pt screen at 3x wants ~1170 device px, and this
 * is a dark backdrop under four black gradient layers, where the last 8% of
 * sharpness is invisible) and the full 1361 for everything larger.
 */
const sharp = require("sharp");
const path = require("path");
const fs = require("fs");

const SRC = path.join(__dirname, "..", "..", "Homepage_background_image.png");
const OUT = path.join(__dirname, "..", "public", "hero");

// Measured. See the header, and the profiles printed at the end of this run.
const CROP_BOTTOM = 33;
const CROP_SIDE = 2;

const WIDTHS = [1080, 1361];

async function main() {
  if (!fs.existsSync(SRC)) {
    console.error(
      `\nMissing source artwork: ${SRC}\n\n` +
        "It is deliberately untracked, so a fresh clone does not have it. The\n" +
        "generated files in public/hero/ are committed and are what the site\n" +
        "serves; this script only needs to run when the artwork changes.\n",
    );
    process.exit(1);
  }

  fs.mkdirSync(OUT, { recursive: true });

  const meta = await sharp(SRC).metadata();
  const region = {
    left: CROP_SIDE,
    top: 0,
    width: meta.width - CROP_SIDE * 2,
    height: meta.height - CROP_BOTTOM,
  };
  const cropped = () => sharp(SRC).extract(region);
  console.log(`source ${meta.width}x${meta.height} -> ${region.width}x${region.height}`);

  for (const w of WIDTHS) {
    const file = path.join(OUT, `home-${w}.webp`);
    await cropped().resize({ width: w }).webp({ quality: 78, effort: 6 }).toFile(file);
    console.log(`  ${path.basename(file)}  ${(fs.statSync(file).size / 1024).toFixed(1)} KB`);
  }

  // One JPEG, for the small tail of clients with no WebP. Not a second srcset —
  // a fallback nobody modern reaches does not need two sizes.
  const jpg = path.join(OUT, "home-1361.jpg");
  await cropped().resize({ width: 1361 }).jpeg({ quality: 78, mozjpeg: true }).toFile(jpg);
  console.log(`  ${path.basename(jpg)}  ${(fs.statSync(jpg).size / 1024).toFixed(1)} KB`);

  // The numbers the crop is justified by, re-printed on every run so that
  // replacing the artwork re-states them rather than inheriting this file's.
  const { data, info } = await sharp(SRC).raw().toBuffer({ resolveWithObject: true });
  const luma = (x, y) => {
    const i = (y * info.width + x) * info.channels;
    return 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
  };
  const rowLuma = (y) => {
    let s = 0;
    for (let x = 0; x < info.width; x++) s += luma(x, y);
    return (s / info.width).toFixed(1);
  };
  console.log("\nrow luma across the bottom edge (the band this crop removes):");
  for (let y = info.height - 36; y < info.height; y += 3) {
    console.log(`  y=${y}  ${rowLuma(y)}${y === info.height - CROP_BOTTOM ? "   <- first row dropped" : ""}`);
  }
}

main();
