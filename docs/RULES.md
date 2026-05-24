# RULES

## General Development Rules

- Keep the project small and focused
- Do not overengineer
- Prefer simple Python solutions
- Prefer readability over cleverness
- Keep changes minimal and reversible
- Never introduce large frameworks without explicit approval

## Safety Rules

- Never hardcode secrets
- Never commit credentials or tokens
- Always respect .gitignore
- Never overwrite existing user files
- Prefer append-only operations when possible
- Always support dry-run for risky operations

## Architecture Rules

- Keep Gmail integration isolated from business logic
- Keep filesystem writing isolated
- Keep runtime modes separated from providers
- Prefer modular design over monolithic scripts
- Avoid tight coupling between modules

## Testing Rules

- Add tests for every meaningful behavior
- Prefer harness testing before real Gmail testing
- Never skip tests for critical file operations
- Keep tests deterministic and local

## Git Workflow Rules

- Work in small branches
- Prefer small commits
- One ticket per focused change
- Do not mix unrelated refactors

## Runtime Rules

- Daily mode must tolerate missed days
- Backfill mode must support resumable execution
- Manifest tracking must prevent duplicate downloads
- File naming must be deterministic and collision-safe

## AI Collaboration Rules

- Claude should prefer small iterative implementations
- Claude should avoid speculative abstractions
- Claude should not introduce unnecessary technologies
- Claude should explain risky architectural changes before implementing them