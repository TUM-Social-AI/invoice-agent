"""
Deployment orchestration for the invoice agent.

This package turns the single-shot CLI into a poll-driven worker suitable for a
scheduled AWS task: it lists new documents from a source folder, claims them so
concurrent/overlapping polls cannot double-process, runs the agent in-process
(models loaded once), uploads results, and records terminal state for dedup.

The source (Google Drive) and the extra LLM provider live on separate branches.
This package depends only on the `DriveClient` protocol in `drive_source`, so it
is fully runnable and testable before those branches merge (see LocalDirDriveClient).
"""
