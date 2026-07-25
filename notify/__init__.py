"""Step 4: turn the jobs table's notified=FALSE queue into one email.

Reads. Sends. Records what it sent. Never crawls, never diffs, never writes a
job row except to stamp it as told.

    match.py    the filter, as pure functions over (job, preferences)
    store.py    the three queries: read preferences, read queue, write receipt
    digest.py   render one grouped email, send it, then record it
"""
