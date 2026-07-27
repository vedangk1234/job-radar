# Git hooks

Enable them once per clone — hooks are not shared by git and a fresh clone has
none:

```sh
git config core.hooksPath scripts/githooks
```

`pre-commit` refuses a commit whose staged changes contain a file named `.env`,
`.env.local` or `scratch.md`, or a literal shaped like a credential (a Resend
key, a Google client secret, an `sk-` API key, the Supabase project ref, or a
`postgres://` URL carrying a real password).

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
