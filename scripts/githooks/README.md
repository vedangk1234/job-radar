# Git hooks

Enable them once per clone — hooks are not shared by git and a fresh clone has
none:

```sh
git config core.hooksPath scripts/githooks
```

`pre-commit` refuses a commit whose staged changes contain:

1. **A forbidden filename** — `.env`, `.env.local` or `scratch.md`.
2. **A credential-shaped literal** — a Resend key, a Google client secret, an
   `sk-` API key, the Supabase project ref, or a `postgres://` URL carrying a
   real password.
3. **Personal data** — a contact address or the committer's own identity, which
   belong in `.env` and get read at run time (see `fetchers/http.py` and
   `dashboard/src/lib/ats.ts`), never baked into a committed file.

Check 3 names nobody, because writing the values here would put them in the
repo — the thing being prevented. It works three ways instead:

- **Structurally**, by flagging any address whose domain is not reserved for
  examples. RFC 2606 and 6761 set aside `example.com`/`.net`/`.org`/`.edu` and
  the `.test`, `.example`, `.invalid` and `.localhost` TLDs precisely so docs and
  fixtures can name an address that cannot be a real person's; anything outside
  those is assumed to be one. (`onboarding@resend.dev` is allowed explicitly — a
  published vendor constant the digest depends on.)
- **From your git identity**, reading `git config user.email` and `user.name` at
  run time, so a fresh clone is protected with no configuration. Tokens under 5
  characters and generic identities (`dev`, `runner`, `github`) are skipped: a
  short name is a substring of too much ordinary text, and a guard that cries
  wolf is one that teaches you to reach for `--no-verify`.
- **From `PERSONAL_DENYLIST`** in the environment, or `git config --add
  jobradar.personalDenylist <value>`, for what the first two miss — an old
  address, or a handle that differs from `user.name`.

Check it works:

```sh
sh scripts/githooks/test-guard.sh
```

The test writes every probe inside a throwaway `.guard-probe/` directory and
invokes the hook directly rather than attempting a commit — see the comment at
the top of that file for why that matters more than it looks.

A hook is not a security boundary. `--no-verify` bypasses it, and it only sees
what is staged. It is there so that leaking a key takes a deliberate act rather
than a forgetful one. If a secret has already reached a remote, rotate it — the
value is burned regardless of what happens to the history.
