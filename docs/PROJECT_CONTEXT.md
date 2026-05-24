# PROJECT CONTEXT

## Project Name

donation-receipts

## Purpose

A local-only Python CLI tool that collects donation receipt attachments from Gmail accounts and organizes them into folders for tax refund preparation.

This project is intentionally small and focused.

## Development Philosophy

The project is designed around safe AI-assisted development with Claude.

Main principles:

- Small iterative tickets
- Harness-first development
- Dry-run support
- Reversible operations
- Minimal architecture
- Secrets outside Git
- Test-first mindset
- Clear separation of responsibilities
- Local-only execution

## Technical Direction

### Included

- Python CLI
- Gmail API
- Local filesystem storage
- Local JSON manifest tracking
- Unit tests
- GitHub Actions CI
- Logging
- Multi-account support

### Explicitly Out of Scope

### Explicitly Out of Scope

- Server
- Frontend/UI
- Database
- Docker
- Kubernetes
- MCP
- Distributed systems
- Heavy agent frameworks
- Cloud deployment
- External storage systems
- Production-scale infrastructure

## Expected Runtime Modes

### Harness Mode

Runs only on local sample data.

### Daily Mode

Runs automatically and checks recent emails.

### Day Recovery Mode

Runs for a specific date to recover missed days.

### Historical Backfill Mode

Runs over large historical date ranges.

## Architecture Constraints

- Keep the project modular but simple
- Prefer plain Python over frameworks
- Keep Gmail provider isolated from business logic
- Keep filesystem writing isolated
- Never tightly couple runtime modes to Gmail implementation

## Important Safety Constraints

- Never hardcode credentials
- Never commit secrets
- Never overwrite user files
- Always support dry-run
- Always track processed messages
- Prefer append-only/reversible behavior

## Expected Project Scale

Small personal automation project.

Do not overengineer.