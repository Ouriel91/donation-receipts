# Donation Receipts

Local Python CLI tool for collecting donation receipt attachments from Gmail and organizing them into folders for tax refund preparation.

This is a small warm-up project before a larger football scouting project.  
The goal is to practice safe AI-assisted development with Claude: small tickets, tests, harness-first development, dry-run mode, logs, and rollback-friendly design.

## Goals

- Find donation-related emails in Gmail
- Detect receipt attachments
- Save attachments into organized folders
- Support multiple Gmail accounts
- Prevent duplicate downloads
- Support daily runs, specific-day recovery, historical backfill, and harness mode
- Keep secrets outside Git
- Keep the project local-only

## Non-Goals

- No server
- No UI
- No database
- No user authentication system
- No Docker for now
- No MCP for now
- No heavy agent framework

## Planned Runtime Modes

```bash
python -m src.main --mode harness
python -m src.main --mode daily
python -m src.main --mode day --date 2026-05-24
python -m src.main --mode backfill --from-date 2021-01-01 --to-date 2026-05-24