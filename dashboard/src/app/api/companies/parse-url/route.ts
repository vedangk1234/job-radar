import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { parseBoardUrl } from "@/lib/board-url";
import { probeBoard } from "@/lib/ats";
import { probeWorkday, WorkdayError } from "@/lib/workday";
import { sql } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 30;

/**
 * Add-by-URL, step 1: take a pasted board URL apart and verify it answers.
 * WRITES NOTHING, same as the resolve route — the confirm step is POST
 * /api/companies.
 *
 * Verification is not optional here. Parsing a URL proves the string is
 * well-formed, not that the board exists: a Workday site name typed one
 * character wrong parses perfectly and then 404s on every crawl, forever. So
 * every parse is followed by one real request before the UI offers to store it.
 */
export async function POST(request: Request) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  let body: Record<string, unknown>;
  try {
    body = await request.json();
  } catch {
    return badRequest("body was not JSON");
  }

  const url = String(body.url ?? "").trim();
  const nameOverride = String(body.name ?? "").trim();
  if (!url) return badRequest("url is required");

  const parsed = parseBoardUrl(url);
  if (parsed.kind === "unknown") {
    return NextResponse.json({ found: false, message: parsed.reason });
  }

  try {
    if (parsed.kind === "tier1") {
      const { count, titles } = await probeBoard(parsed.ats, parsed.token);
      if (count === 0) {
        return NextResponse.json({
          found: false,
          message:
            `Parsed that as ${parsed.ats}:${parsed.token}, but the board answered with no ` +
            `jobs. Either the token is wrong or the board is genuinely empty — and an empty ` +
            `board is indistinguishable from a dead one, so this will not store it.`,
        });
      }
      const existing = await lookup(parsed.ats, parsed.token);
      return NextResponse.json({
        found: true,
        candidate: {
          name: nameOverride || parsed.token,
          ats: parsed.ats,
          token: parsed.token,
          config: {},
          category: "unknown",
          india_scoped: false,
        },
        jobCount: count,
        sampleTitles: titles.slice(0, 5),
        existing,
      });
    }

    // --- Workday ---------------------------------------------------------
    const config = parsed.config;
    if (config.ambiguous?.length) {
      return NextResponse.json({
        found: false,
        parsed: config,
        message:
          `That URL parsed ambiguously (${config.ambiguous.join(", ")}). Workday vanity ` +
          `domains hide the tenant, so the guess is not trustworthy. Open the careers page, ` +
          `find the /wday/cxs/{tenant}/{site}/jobs request it makes, and paste that URL.`,
      });
    }

    const probe = await probeWorkday(config);
    // What gets stored: the four keys the fetcher reads, nothing else.
    const stored = Object.fromEntries(
      (["host", "site", "lang", "careers_url"] as const)
        .map((k) => [k, config[k]])
        .filter(([, v]) => v),
    );

    return NextResponse.json({
      found: true,
      candidate: {
        name: nameOverride || config.tenant,
        ats: "workday",
        token: config.tenant, // for workday the token IS the tenant
        config: stored,
        category: "gcc",
        india_scoped: probe.indiaScoped,
      },
      jobCount: probe.boardTotal,
      // The Workday probe is deliberately UNFILTERED, so this total is the
      // tenant's whole worldwide board — not what will actually be stored.
      // Labelling it "jobs on board" next to an india-scoped badge would read
      // as "2000 Indian jobs", which is off by an order of magnitude.
      countLabel: "jobs on board (worldwide, unfiltered)",
      sampleTitles: probe.sampleTitles,
      existing: await lookup("workday", config.tenant ?? ""),
      note: probe.indiaScoped
        ? `India facet "${probe.facet}" found — the crawler will filter at source, so it will ` +
          `store the India subset of those, not all of them. Marked india_scoped.`
        : `No India facet on this tenant. Stored india_scoped=false; the crawler will fetch a ` +
          `capped slice and filter by city label instead.`,
    });
  } catch (err) {
    if (err instanceof WorkdayError) {
      return NextResponse.json({
        found: false,
        message: `That tenant did not answer with a board — ${err.message}`,
      });
    }
    return serverError(err, "POST /api/companies/parse-url");
  }
}

async function lookup(ats: string, token: string) {
  const [row] = await sql<{ id: number; name: string; tracked: boolean }[]>`
    SELECT id, name, tracked FROM companies WHERE ats = ${ats} AND token = ${token}
  `;
  return row ?? null;
}
