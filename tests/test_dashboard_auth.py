"""What Phase 2a's auth must not quietly become — asserted over the tree.

Every rule here is one whose violation produces NO symptom in development. The
app boots, the pages render, the operator signs in and everything looks right;
the failure is only visible to somebody who is not the operator, or to somebody
holding a forged cookie, and neither of those opens a pull request.

That is the same argument tests/test_dashboard_notified.py makes about a column
name, applied to a security boundary instead. Four of the five rules below
replaced something that was true in Phase 1 and stopped being true when the
ALLOWED_EMAIL allowlist came off, so each is a place where old code and new code
disagree about who is allowed to be here.

These are TEXTUAL assertions over the source. They cannot prove the gate works —
`npm run ui-check` asks the running server that, unauthenticated, over real
HTTP. They prove the shapes that make it possible to reason about the gate at
all.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "dashboard" / "src"
SCRIPTS = PROJECT_ROOT / "dashboard" / "scripts"


def sources(root: Path) -> dict[Path, str]:
    """Every TypeScript source under root, by path."""
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.ts*"))
        if path.is_file()
    }


def code_lines(text: str) -> list[str]:
    """Lines with `//` comments stripped and block-comment bodies dropped.

    The files in this project explain themselves at length, and every rule below
    is discussed by name in prose somewhere — lib/guard.ts talks about
    ALLOWED_EMAIL, lib/supabase/server.ts talks about getSession. Matching raw
    text would fail on a comment that says "we do not do this", which is exactly
    backwards: it would punish the file for documenting the rule.
    """
    out: list[str] = []
    in_block = False
    for raw in text.splitlines():
        line = raw
        if in_block:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_block = False
            else:
                continue
        while "/*" in line:
            before, rest = line.split("/*", 1)
            if "*/" in rest:
                line = before + rest.split("*/", 1)[1]
            else:
                line = before
                in_block = True
                break
        line = line.split("//", 1)[0]
        if line.strip():
            out.append(line)
    return out


def test_nothing_calls_getSession():
    """getUser() verifies the JWT; getSession() decodes a cookie and trusts it.

    On a server the cookie arrived over the network, so getSession() means
    anybody who can set a cookie is anybody they claim to be. There is no
    legitimate server-side use in this app — not in a gate, not in the layout
    deciding whether to draw the nav — because an exception is the thing the next
    person copies.
    """
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path, text in {**sources(SRC), **sources(SCRIPTS)}.items()
        for line in code_lines(text)
        if "getSession" in line
    ]
    assert not offenders, "getSession() trusts an unverified cookie:\n  " + "\n  ".join(offenders)


def test_the_allowlist_is_gone_from_executable_code():
    """ALLOWED_EMAIL was the Phase-1 lock and 2a removes it, not softens it.

    A surviving read would be a second gate nobody remembers — sign-up would
    appear to work and then refuse everyone but the operator, with the refusal
    coming from a variable the sign-in page no longer mentions.
    """
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path, text in {**sources(SRC), **sources(SCRIPTS)}.items()
        for line in code_lines(text)
        if "ALLOWED_EMAIL" in line
    ]
    assert not offenders, "ALLOWED_EMAIL still read:\n  " + "\n  ".join(offenders)


def test_next_auth_is_gone():
    """One session system. Two would be two answers to "who is this"."""
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path, text in {**sources(SRC), **sources(SCRIPTS)}.items()
        for line in code_lines(text)
        if "next-auth" in line
    ]
    assert not offenders, "next-auth is still imported:\n  " + "\n  ".join(offenders)


def test_the_request_path_never_resolves_identity_from_MY_EMAIL():
    """THE ONE THAT MATTERS MOST, and what 2b turned it into.

    currentUserId() resolves the OPERATOR from an environment variable. Through
    Phase 1 that was the only answer available and every page used it. From 2a the
    request path takes its user from the session instead — because with sign-up
    open, a page that still called it would serve the operator's filter, watch
    list and job history to whichever stranger happened to be signed in, and it
    would look completely normal to the operator.

    2a left the file in src/lib and exempted it here. 2b moved it to
    dashboard/scripts/current-user.ts, where the things with no session live, so
    this assertion has NO exemption any more: not one line under src/ may name
    the function, because there is nothing under src/ that defines it.
    """
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path, text in sources(SRC).items()
        for line in code_lines(text)
        if "currentUserId" in line
    ]
    assert not offenders, (
        "the request path resolves identity from MY_EMAIL instead of the session:\n  "
        + "\n  ".join(offenders)
    )


def test_the_identity_bridge_is_not_importable_from_the_request_path():
    """The file is gone from src/, which is the half a text search cannot state.

    A rule saying "do not call this" is one careless import from being broken by
    somebody who needs a user id in a hurry and finds a module in lib/ that
    supplies one. A module that is not in lib/ cannot be found that way — and
    `@/lib/...` is the only import alias the app has, so a page reaching into
    dashboard/scripts/ would have to write a relative path out of src/ to do it.
    """
    assert not (SRC / "lib" / "currentUser.ts").exists(), (
        "src/lib/currentUser.ts is back: the MY_EMAIL lookup belongs in "
        "dashboard/scripts/current-user.ts, where the session-less checks live"
    )
    assert (SCRIPTS / "current-user.ts").exists(), (
        "dashboard/scripts/current-user.ts is missing — badge-check and ui-check "
        "have no way to name the operator's account"
    )

    reaching_out = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path, text in sources(SRC).items()
        for line in code_lines(text)
        if "current-user" in line or ("import" in line and "scripts/" in line)
    ]
    assert not reaching_out, (
        "a module under src/ imports from dashboard/scripts/:\n  " + "\n  ".join(reaching_out)
    )


def test_MY_EMAIL_is_read_under_src_only_for_the_outbound_user_agent():
    """MY_EMAIL survives in the app, but not as an answer to "who is this".

    The variable was always two things wearing one name: a contact address for
    the outbound User-Agent (lib/ats.ts, falling back to unknown@example.com
    because an unset one only makes a probe impolite) and, from Phase 1b, the
    identity the current user was resolved from. 2b removes the second reading
    from this runtime entirely — including lib/env.myEmail(), whose only caller
    was the lookup that moved out — and this enumerates what may still read the
    variable so that a new reader has to argue with a test.
    """
    allowed = {SRC / "lib" / "ats.ts"}
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path, text in sources(SRC).items()
        if path not in allowed
        for line in code_lines(text)
        if "MY_EMAIL" in line or "myEmail" in line
    ]
    assert not offenders, (
        "MY_EMAIL is read under src/ outside the User-Agent path:\n  " + "\n  ".join(offenders)
    )


def test_every_api_route_guards_itself():
    """Belt and braces with the middleware matcher, which is a regex.

    Middleware protection is one careless edit from a hole nothing tests, so a
    route must be readable in isolation and seen to be protected.
    """
    routes = [p for p in SRC.rglob("route.ts") if "api" in p.parts]
    assert routes, "no API routes found — this test would pass vacuously"

    unguarded = [
        str(path.relative_to(PROJECT_ROOT))
        for path in routes
        # auth/callback is the code exchange and MUST work signed out; it is the
        # one route whose whole job is to run before a session exists.
        if "callback" not in path.parts
        and "requireSession" not in path.read_text(encoding="utf-8")
    ]
    assert not unguarded, "API route(s) with no requireSession():\n  " + "\n  ".join(unguarded)


def test_every_page_guards_itself():
    """Including /add and /preferences, which did not until 2a.

    Both leaned on the matcher alone, and /preferences read the database on the
    server while doing it. The rule "every page checks for itself" had two silent
    exceptions, which is the same as not having the rule.
    """
    pages = list((SRC / "app").rglob("page.tsx"))
    assert pages, "no pages found — this test would pass vacuously"

    unguarded = [
        str(path.relative_to(PROJECT_ROOT))
        for path in pages
        # /signin is the one page reachable while signed out.
        if path.parent.name != "signin" and "requirePage" not in path.read_text(encoding="utf-8")
    ]
    assert not unguarded, "page(s) with no requirePage():\n  " + "\n  ".join(unguarded)


def test_the_onboarding_gate_is_closed_by_default_and_has_exactly_three_holes():
    """Phase 3 Slice 1b's gate, asserted the way the middleware matcher is.

    requirePage() redirects a signed-in account whose preferences row says
    onboarding_completed = FALSE. Being inside requirePage rather than beside it
    is what makes a page added next month gated by DEFAULT — the same
    allow-nothing-with-named-holes direction middleware.ts chose, for the same
    reason: forgetting to register a new page cannot open a hole, only forgetting
    to exempt one can close a door.

    So the thing that can go wrong is the exemption spreading, and it spreads by
    being copied. Three files may pass it, each for a stated reason:

      app/onboarding/page.tsx — the redirect target; gated it redirects to itself
      app/preferences/page.tsx — DELETE ACCOUNT lives there, and erasure is a
                                 right that does not wait on a filter
      app/actions.ts — the same argument for the form post itself

    A fourth is a page that has decided onboarding is optional, which is the
    whole gate gone one file at a time.
    """
    flag = "allowUnfinishedOnboarding"
    guard = (SRC / "lib" / "guard.ts").read_text(encoding="utf-8")
    body = "\n".join(code_lines(guard))

    # THREE FACTS, NOT ONE, and the first draft of this test asserted the wrong
    # one. It looked for the name `onboardingCompleted` anywhere in the code, and
    # deleting the entire redirect still left the IMPORT of that name at the top
    # of the file — so the gate could be removed wholesale with the test still
    # green. A mutation run is what turned that up. The three below name the call,
    # the consequence and the condition separately, because a gate can be broken
    # by removing any one of them: a read whose answer is discarded, a redirect
    # nothing triggers, or a condition that ignores the exemption.
    assert "onboardingCompleted(" in body, (
        "requirePage() does not CALL onboardingCompleted — importing the name is "
        "not reading the flag, and an unused import is exactly what a deleted gate "
        "leaves behind"
    )
    assert "redirect(ONBOARDING_PATH)" in body, (
        "nothing redirects to ONBOARDING_PATH — the flag is read and the answer thrown away"
    )
    assert f"!{flag}" in body, (
        f"the redirect is not conditioned on !{flag}, so either every page is exempt "
        f"or none can be"
    )
    assert re.search(rf"{flag}\s*=\s*false", guard), (
        f"{flag} must DEFAULT to false, or every page is exempt unless it opts in"
    )

    allowed = {
        SRC / "app" / "onboarding" / "page.tsx",
        SRC / "app" / "preferences" / "page.tsx",
        SRC / "app" / "actions.ts",
        SRC / "lib" / "guard.ts",  # where the parameter is declared
    }
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {line.strip()}"
        for path, text in sources(SRC).items()
        if path not in allowed
        for line in code_lines(text)
        if flag in line
    ]
    assert not offenders, (
        "a file outside the three enumerated holes opts out of the onboarding gate:\n  "
        + "\n  ".join(offenders)
    )


def test_the_onboarding_target_is_a_real_page_that_guards_itself():
    """The redirect must land somewhere that exists and is behind the session.

    A redirect to a missing route is a 404 for every new account, and the page it
    lands on is the one page exempt from the onboarding gate — which is exactly
    the file where an over-wide exemption would also drop the SESSION gate. It
    calls requirePage() like every other page; test_every_page_guards_itself
    covers that, and this states the pairing so the two cannot be separated by
    someone renaming the route.
    """
    guard = (SRC / "lib" / "guard.ts").read_text(encoding="utf-8")
    target = re.search(r'ONBOARDING_PATH\s*=\s*"(/[^"]*)"', guard)
    assert target, "lib/guard.ts does not declare ONBOARDING_PATH as a literal path"

    page = SRC / "app" / target.group(1).strip("/") / "page.tsx"
    assert page.exists(), (
        f"requirePage() redirects to {target.group(1)}, but {page.relative_to(PROJECT_ROOT)} "
        f"does not exist — every new account would get a 404"
    )
    assert "requirePage" in page.read_text(encoding="utf-8"), (
        "the onboarding page is exempt from the ONBOARDING gate, not from the session gate"
    )


def test_server_errors_do_not_leak_the_message_to_the_client():
    """The "one authorised user, they are the operator" argument expired in 2a.

    This project's errors name tables, columns, migration scripts and
    environment variables. That is a feature for an operator reading their own
    500 and reconnaissance for a stranger who signed up ninety seconds ago.
    """
    guard = (SRC / "lib" / "guard.ts").read_text(encoding="utf-8")
    body = "\n".join(code_lines(guard))
    assert "err instanceof Error ? err.message" not in body, (
        "serverError() is putting the raw error message back in the response body"
    )
    assert "console.error" in body, "serverError() must still log the detail server-side"


def test_the_link_adopts_by_lowercased_email():
    """The branch that saves the Phase-1 account, asserted rather than trusted.

    public.users.id for the existing account came from gen_random_uuid() and can
    never equal an auth uid, so email is the only thing that can connect that
    person to their 50 companies and 7,708 notification rows. lower() on both
    sides matches users_email_lower_idx — without it an address differing only in
    case creates a second account and the first becomes unreachable.
    """
    link = (SRC / "lib" / "link-user.ts").read_text(encoding="utf-8")
    body = "\n".join(code_lines(link))

    assert re.search(r"UPDATE public\.users", body), "no adoption UPDATE in the link function"
    assert re.search(r"lower\(email\)\s*=\s*lower\(", body), (
        "the adoption branch must compare lower(email) on BOTH sides"
    )
    assert "ON CONFLICT DO NOTHING" in body, (
        "the insert branch needs ON CONFLICT for two concurrent first sign-ins"
    )
    assert re.search(r"is_active", body), (
        "a new account must be created is_active = FALSE until 2d verifies a sending domain"
    )
    assert "emailIsVerified" in body, "the link must refuse an unverified address"
