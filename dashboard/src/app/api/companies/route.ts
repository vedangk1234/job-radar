import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import { listCompanyCards, upsertCompany } from "@/lib/companies";

export const runtime = "nodejs"; // postgres.js needs real TCP sockets
export const dynamic = "force-dynamic";

/**
 * Every company with both counts — `open_jobs` and `match_jobs`, the two
 * numbers on a row. Counts only: the job rows behind them stay on the server
 * until a row is expanded and asks /api/companies/:id/jobs for them.
 */
export async function GET() {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  try {
    return NextResponse.json({ companies: await listCompanyCards() });
  } catch (err) {
    return serverError(err, "GET /api/companies");
  }
}

const VALID_ATS = new Set([
  "greenhouse",
  "lever",
  "ashby",
  "smartrecruiters",
  "workable",
  "recruitee",
  "workday",
]);

/**
 * Confirm-add. The last step of both add paths: the client has already been
 * shown what the probe found and is now saying yes to it.
 *
 * This route does NOT re-probe. That is deliberate — the probe result the human
 * approved is the thing being stored, and re-fetching here would open a window
 * where what you confirmed and what got saved are different boards.
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

  const name = String(body.name ?? "").trim();
  const ats = String(body.ats ?? "").trim().toLowerCase();
  const token = String(body.token ?? "").trim();

  if (!name) return badRequest("name is required");
  if (!VALID_ATS.has(ats)) return badRequest(`unknown ats "${ats}"`);
  if (!token) return badRequest("token is required");

  const config =
    body.config && typeof body.config === "object" && !Array.isArray(body.config)
      ? (body.config as Record<string, unknown>)
      : {};

  // A Workday row with no host/site is a crawl that fails every 30 minutes
  // forever — the same refusal add_workday.py makes before it stores anything.
  if (ats === "workday" && (!config.host || !config.site)) {
    return badRequest("a workday company needs config.host and config.site");
  }

  try {
    const { id, inserted } = await upsertCompany({
      name,
      ats,
      token,
      config,
      category: String(body.category ?? "unknown").trim() || "unknown",
      indiaScoped: body.india_scoped === true,
    });
    return NextResponse.json({
      id,
      inserted,
      message: inserted
        ? `Added ${name} (${ats}:${token}), tracked.`
        : `${name} was already tracked as ${ats}:${token} — the row was refreshed, not duplicated.`,
    });
  } catch (err) {
    return serverError(err, "POST /api/companies");
  }
}
