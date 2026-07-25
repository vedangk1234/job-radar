"""Async Postgres connection helper for the job-radar crawler.

Everything here talks to Supabase through the SESSION POOLER
(aws-0-<region>.pooler.supabase.com:5432, user postgres.<ref>) rather than the
direct db.<ref>.supabase.co endpoint, which is IPv6-only and therefore
unreachable from GitHub Actions runners.
"""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# job-radar/db/connection.py -> job-radar/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_env() -> None:
    """Load job-radar/.env. Existing process env vars win (CI sets them directly)."""
    load_dotenv(ENV_PATH, override=False)


def get_crawler_dsn() -> str:
    """Return CRAWLER_DB_URL, with sanity checks for the usual Supabase footguns."""
    load_env()
    dsn = os.getenv("CRAWLER_DB_URL", "").strip()

    if not dsn:
        raise RuntimeError(
            f"CRAWLER_DB_URL is not set.\n"
            f"Copy {PROJECT_ROOT / '.env.example'} to {ENV_PATH} and fill in real values."
        )
    if "<ref>" in dsn or "<password>" in dsn or "<region>" in dsn:
        raise RuntimeError(
            "CRAWLER_DB_URL still contains placeholders from .env.example. "
            "Fill in your real project ref, password, and region."
        )
    if ".supabase.co" in dsn and ".pooler.supabase.com" not in dsn:
        print(
            "WARNING: CRAWLER_DB_URL looks like the direct db.<ref>.supabase.co endpoint.\n"
            "         That host is IPv6-only and will fail on GitHub Actions (IPv4-only).\n"
            "         Use the session pooler: aws-0-<region>.pooler.supabase.com:5432",
            file=sys.stderr,
        )
    return dsn


def _ssl_context() -> ssl.SSLContext:
    """Build the SSL context. Supabase refuses unencrypted connections.

    The pooler presents a `*.pooler.supabase.com` certificate signed by Supabase's
    own private CA ("Supabase Intermediate 2021 CA"), which is not in any system
    trust store — so a default verifying context fails with
    CERTIFICATE_VERIFY_FAILED / "self-signed certificate in certificate chain".

    Default: encrypted but unverified, i.e. libpq `sslmode=require`. This is what
    Supabase's own connection strings use.

    To harden: download your project's CA from Supabase (Project Settings ->
    Database -> SSL Configuration -> Download certificate) and point
    SUPABASE_CA_CERT at it. Verification then happens for real.
    """
    ca_path = os.getenv("SUPABASE_CA_CERT", "").strip()
    if ca_path:
        ctx = ssl.create_default_context(cafile=ca_path)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def get_crawler_conn() -> asyncpg.Connection:
    """Open a single asyncpg connection to Supabase via the session pooler.

    statement_cache_size=0 is required: the pooler can hand the same client a
    different backend, which makes asyncpg's cached prepared statements blow up
    with DuplicatePreparedStatementError / InvalidSQLStatementNameError.
    """
    load_env()
    return await asyncpg.connect(
        get_crawler_dsn(),
        ssl=_ssl_context(),
        statement_cache_size=0,
    )
