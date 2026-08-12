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
 * Start watching one company.
 *
 * It also recorded that company's stored jobs as already-seen, in the same
 * transaction, until the filter-first slice — because watching a company was what
 * admitted its history to a digest. It is not any more, and suppressing on the
 * strength of a pin would take away jobs the person's filter had already earned
 * them; see lib/watchlist.watchCompany. This handler validates an integer and
 * reports what happened, as it always did.
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
       * A `suppressed` count travelled here until the filter-first slice, and the
       * argument for reporting it to the SCREEN rather than to a log has simply
       * moved rather than died: "Now watching Adobe" said nothing about the 214
       * stored postings that had just become history, and a person who was not
       * told would reasonably expect them in the morning. Watching a company no
       * longer withholds anything — see lib/watchlist.watchCompany — so there is
       * nothing to report here. The same sentence is now carried by PUT
       * /api/preferences, where the withholding actually happens.
       */
      message: result.inserted
        ? "Now watching it. Your filter already covers every board we crawl — pinning is " +
          "for keeping this one in view."
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
 * It removes a pin and nothing else. There was a paragraph here about the
 * suppression rows deliberately outliving the pair — removing them would have made
 * an unwatch/re-watch cycle re-deliver everything held in between — and it went
 * with the suppression itself in the filter-first slice.
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
        ? "Unpinned. Its jobs still match your filter like any other board's — this only " +
          "takes it off your pinned list."
        : "You were not watching it — nothing changed.",
    });
  } catch (err) {
    return serverError(err, "DELETE /api/watchlist");
  }
}
