#!/bin/sh
#
# Prove the secret guard blocks what it claims to block.
#
#     sh scripts/githooks/test-guard.sh
#
# SAFETY, WHICH IS THE WHOLE POINT OF HOW THIS IS WRITTEN
# -------------------------------------------------------
# The obvious way to test a hook that blocks a file called `.env` is to create a
# file called `.env` and try to commit it. Do that in the repo root and you
# overwrite the real one. This was not a hypothetical: it happened, and .env is
# gitignored, so there was no committed copy to restore from.
#
# So two rules here, both structural rather than careful:
#
#   1. Every probe lives under ONE throwaway directory. The filename rules match
#      on BASENAME, so `.guard-probe/.env` exercises the `.env` rule exactly
#      while being a path nothing real can occupy.
#   2. It runs the hook DIRECTLY instead of running `git commit`. The hook reads
#      the index and exits 0 or 1; invoking it is the whole test. No commit is
#      ever attempted, so no commit can ever half-happen.
#
# It also refuses to start if the probe directory already exists, rather than
# assuming it is safe to delete something it did not create.

set -u

cd "$(git rev-parse --show-toplevel)" || exit 1

HOOK=scripts/githooks/pre-commit
PROBE=.guard-probe

if [ ! -x "$HOOK" ] && [ ! -f "$HOOK" ]; then
	echo "cannot find $HOOK" >&2
	exit 1
fi
if [ -e "$PROBE" ]; then
	echo "$PROBE already exists — refusing to touch it. Remove it and re-run." >&2
	exit 1
fi

# The hook judges the WHOLE index, not just the file under test, so a violation
# already staged would make every case come back "block" — including the ones
# that must be allowed. That reads as five mysterious failures in the guard
# rather than one obvious problem in the index, so check it up front and say so.
if ! sh "$HOOK" >/dev/null 2>&1; then
	echo "The index ALREADY fails the guard, before any probe was added." >&2
	echo "That is a real finding — inspect it — but it also makes this test" >&2
	echo "meaningless, because every case would come back blocked. Details:" >&2
	echo >&2
	sh "$HOOK" >&2
	exit 1
fi

mkdir -p "$PROBE"
# Whatever happens next, the probes come out of the index and off the disk.
trap 'git restore --staged "$PROBE" >/dev/null 2>&1; rm -rf "$PROBE"' EXIT INT TERM

pass=0
fail=0

# want=block | want=allow
case_() {
	want=$1
	name=$2
	content=$3

	path="$PROBE/$name"
	mkdir -p "$(dirname "$path")"
	printf '%s\n' "$content" > "$path"
	git add -f "$path" >/dev/null 2>&1

	if sh "$HOOK" >/dev/null 2>&1; then
		got=allow
	else
		got=block
	fi

	git restore --staged "$path" >/dev/null 2>&1
	rm -f "$path"

	if [ "$got" = "$want" ]; then
		pass=$((pass + 1))
		printf '  ok    %-7s %s\n' "$want" "$name"
	else
		fail=$((fail + 1))
		printf '  FAIL  wanted %s, got %s: %s\n' "$want" "$got" "$name"
	fi
}

# The probe strings are assembled from halves the shell rejoins at runtime, for
# the same reason the hook does it to its own patterns: a test file that spells
# out the shapes it is testing is a file the guard refuses to let you commit.
# The probes are still byte-for-byte the real shapes by the time they hit disk —
# nothing is weakened, only the source text is broken up.
#
# None of these are live credentials. They are the right SHAPE and nothing else.
P_RESEND='re''_FaKeKeyAbCdEfGhIjKlMnOpQrSt'
P_RESEND2='re''_AbCdEfGhIjKlMnOpQrStUv'
P_GOOGLE='GOCSP''X-notarealsecretvalue'
P_OPENAI='sk''-proj-AbCdEfGhIjKlMnOpQrStUvWx'
P_SUPA='qfbegmglknn''oyfrgoouc'
# Split inside the SCHEME, not inside the password: the pattern only requires
# the password to be non-placeholder, so breaking it there still matches and
# the file still cannot be committed. `postgresql:` + `//` is the one seam the
# regex cannot bridge.
P_PG='postgresql:''//postgres.abc:Hunter2Password@aws-0-x.pooler.supabase.com:6543/postgres'

echo "secret shapes (must be blocked):"
case_ block resend.txt    "RESEND_API_KEY=$P_RESEND"
case_ block google.txt    "GOOGLE_CLIENT_SECRET=$P_GOOGLE"
case_ block openai.txt    "key = \"$P_OPENAI\""
case_ block supabase.txt  "https://$P_SUPA.supabase.co/rest/v1"
case_ block pgurl.txt     "$P_PG"
case_ block nested/deep/leak.md "see $P_RESEND2 for the key"

echo
echo "forbidden filenames (must be blocked, even with harmless contents):"
case_ block .env          'nothing secret in here at all'
case_ block .env.local    'nothing secret in here at all'
case_ block scratch.md    'nothing secret in here at all'
case_ block sub/.env      'a forbidden name in a subdirectory is still forbidden'

echo
echo "personal data (must be blocked):"
# The split is BEFORE the '@' rather than inside the local part, because the
# email rule needs at least one local-part character immediately left of the
# '@'. A bare quote there matches nothing, so this source file is committable
# while the file it writes to disk is byte-for-byte a real address.
P_MAIL='someone.real''@gmail.com'
P_MAIL2='hire''@acme-corp.co.in'
case_ block contact.ts    "const UA = \"job-radar/0.1 (+$P_MAIL)\";"
case_ block nested/ua.py  "email = \"$P_MAIL2\""

# Rule B, and the reason it needs no fixture: the values come from the git
# identity at run time, so the probe is generated rather than written down and
# this file still names nobody. Skipped rather than failed when git has no
# identity configured — a fresh CI clone often has none, and a skip that says
# so is honest where a pass would not be.
IDENT_MAIL=$(git config user.email 2>/dev/null)
IDENT_NAME=$(git config user.name 2>/dev/null)
if [ -n "$IDENT_MAIL" ]; then
	case_ block identity-email.md "contact: $IDENT_MAIL"
else
	echo "  skip  block   identity-email.md (git config user.email unset)"
fi
if [ -n "$IDENT_NAME" ] && [ "${#IDENT_NAME}" -ge 5 ]; then
	case_ block identity-name.md "author: $IDENT_NAME"
else
	echo "  skip  block   identity-name.md (git config user.name unset or under 5 chars)"
fi

# Rule C, exercised through the environment the same way it is meant to be used.
# Exported explicitly rather than as a `VAR=x case_ ...` prefix: POSIX leaves
# what that means for a FUNCTION unspecified, and the value has to reach a
# grandchild process (case_ runs `sh "$HOOK"`), which needs a real export.
PERSONAL_DENYLIST='oldhandle42'
export PERSONAL_DENYLIST
case_ block denylist.txt  'see github.com/oldhandle42/x'
case_ allow notdeny.txt   'see github.com/someoneelse/x'
unset PERSONAL_DENYLIST

echo
echo "false positives (must be ALLOWED — a guard that cries wolf gets bypassed):"
case_ allow fixture.py    'monkeypatch.setenv("MY_EMAIL", "me@example.test")'
case_ allow fallback.ts   'process.env.MY_EMAIL?.trim() || "unknown@example.com"'
case_ allow template.txt  'ALLOWED_EMAIL=you@example.com'
# Resend's sandbox sender is a vendor constant the digest genuinely depends on.
case_ allow sandbox.py    'SANDBOX_FROM = "onboarding@resend.dev"'
case_ allow localpart.md  'the local part before the @ is called the local part'
case_ allow prose.md      'risk-free task-runner disk-usage: none of these are sk- keys'
case_ allow example.txt   'DASHBOARD_DB_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres'
case_ allow shortre.txt   'RESEND_API_KEY=re_xxxxxxxx'
case_ allow envexample/.env.example 'CRAWLER_DB_URL=postgresql://postgres.<ref>:<password>@host:5432/postgres'
case_ allow code.ts       'const sql = postgres(url, { prepare: false });'

echo
echo "the guard must not block its own source:"
git add -f "$HOOK" >/dev/null 2>&1
if sh "$HOOK" >/dev/null 2>&1; then
	pass=$((pass + 1))
	echo "  ok    allow   $HOOK"
else
	fail=$((fail + 1))
	echo "  FAIL  the guard refuses to let itself be committed"
fi
git restore --staged "$HOOK" >/dev/null 2>&1

echo
if [ "$fail" -ne 0 ]; then
	echo "$fail of $((pass + fail)) checks FAILED"
	exit 1
fi
echo "all $pass checks passed"
