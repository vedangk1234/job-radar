import { NextResponse } from "next/server";

import { badRequest, requireSession, serverError } from "@/lib/guard";
import {
  UnknownCompanyError,
  WatchlistFullError,
  unwatchCompany,
  watchCompany,
  watchlistState,
} from "@/lib/watchlist";

export const runtime = "nodejs"; // postgres.js needs real TCP sockets
export const dynamic = "force-dynamic";

/**
 * The watch list, per person — the write path this app has never had.
 *
 * Everything here is scoped to `gate.session.userId` and nothing takes a user id
 * from the request, which is what makes this route unable to subscribe anybody
 * but the caller. That is the difference between this and the control Slice 1a
 * removed: `companies.tracked` is one shared value for the whole install, so a
 * signed-in stranger writing it silenced everybody's crawl; a row here is a
 * statement about the person making the request and about nobody else.
 *
 * THE ID TRAVELS IN THE BODY RATHER THAN THE PATH, unlike /api/companies/:id. It
 * is one route for both directions of one act, and a DELETE with a body is
 * ordinary; splitting it into /api/watchlist/[id] would put the two halves of
 * "watch / unwatch" in different files for no gain. (Contrast /api/jobs/applied,
 * where the body is forced: a canonical_id can contain an encoded slash and would
 * 404 in the router before any handler ran.)
 */

/** Whatever the caller sent, as a company id — or null if it was not one. */
function companyIdFrom(body: Record<string, unknown>): number | null {
  const id = Number(body.company_id);
  return Number.isInteger(id) && id > 0 ? id : null;
}

async function readBody(request: Request): Promise<Record<string, unknown> | null> {
  try {
    return (await request.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** What this account watches, and the limit it watches under. */
export async function GET() {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  try {
    const state = await watchlistState(gate.session.userId);
    return NextResponse.json({
      company_ids: state.companyIds,
      watching: state.companyIds.length,
      cap: state.cap,
      plan: state.plan,
    });
  } catch (err) {
    return serverError(err, "GET /api/watchlist");
  }
}

/**
 * Start watching one company — and, in the same transaction, record its stored
 * jobs as already-seen so the first digest is not an archive dump.
 *
 * The obligation is discharged in lib/watchlist.watchCompany rather than here,
 * deliberately: a route that did half of it and called a plain INSERT for the
 * other would be the loaded gun db/watchlist.py deleted its bulk statement to
 * avoid. This handler validates an integer and reports what happened.
 *
 * 409 RATHER THAN 400 FOR THE CAP, and rather than 402. The request is
 * well-formed — `{"company_id": 12}` is exactly right — and what refuses it is
 * the state of the account, which is what 409 Conflict means. 400 would say the
 * caller made a mistake they can fix by rewriting the request, and they cannot.
 * 402 Payment Required would be a lie today: there is nothing to pay, the paid
 * tier is a pricing page and the landing copy says so.
 */
export async function POST(request: Request) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  const body = await readBody(request);
  if (!body) return badRequest("body was not JSON");

  const companyId = companyIdFrom(body);
  if (companyId === null) {
    return badRequest("company_id is required and must be a positive integer");
  }

  try {
    const result = await watchCompany(gate.session.userId, companyId);
    const state = await watchlistState(gate.session.userId);
    return NextResponse.json({
      company_id: companyId,
      watching: result.watching,
      cap: state.cap,
      company_ids: state.companyIds,
      /*
       * The suppression count is reported to the SCREEN and not only to a log,
       * which is the one piece of UI opinion in this route. "Now watching Adobe"
       * says nothing about the 214 stored postings that just became history; a
       * person who was not told would reasonably expect them in the morning, and
       * a digest that then contains three jobs looks broken rather than correct.
       */
      suppressed: result.suppressed,
      message: result.inserted
        ? `Now watching it. Its ${result.suppressed} stored job(s) were marked as ` +
          `already seen, so your first digest carries only postings that arrive ` +
          `from now on.`
        : "You were already watching it — nothing changed.",
    });
  } catch (err) {
    if (err instanceof WatchlistFullError) {
      return NextResponse.json(
        { error: "watchlist_full", detail: err.message, cap: err.cap, watching: err.watching },
        { status: 409 },
      );
    }
    if (err instanceof UnknownCompanyError) {
      return NextResponse.json({ error: "not_found", detail: err.message }, { status: 404 });
    }
    return serverError(err, "POST /api/watchlist");
  }
}

/**
 * Stop watching one company.
 *
 * The suppression rows stay — see lib/watchlist.unwatchCompany for why removing
 * them would make an untrack/re-track cycle re-deliver everything in between.
 */
export async function DELETE(request: Request) {
  const gate = await requireSession();
  if (!gate.ok) return gate.response;

  const body = await readBody(request);
  if (!body) return badRequest("body was not JSON");

  const companyId = companyIdFrom(body);
  if (companyId === null) {
    return badRequest("company_id is required and must be a positive integer");
  }

  try {
    const removed = await unwatchCompany(gate.session.userId, companyId);
    const state = await watchlistState(gate.session.userId);
    return NextResponse.json({
      company_id: companyId,
      removed,
      watching: state.companyIds.length,
      cap: state.cap,
      company_ids: state.companyIds,
      message: removed
        ? "Stopped watching it. Its jobs will not appear in your digest or on Home."
        : "You were not watching it — nothing changed.",
    });
  } catch (err) {
    return serverError(err, "DELETE /api/watchlist");
  }
}
