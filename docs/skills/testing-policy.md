# Skill: Testing Policy

## Purpose

Define the testing philosophy and expectations for this project.

The goal is to keep tests reliable, local, deterministic, and safe.

---

## Core Testing Principles

- Prefer small focused tests
- Prefer deterministic behavior
- Prefer local-only tests
- Prefer fake data over real integrations
- Avoid flaky tests
- Avoid hidden external dependencies

---

## Harness-First Philosophy

Business logic should usually be tested in harness mode before real Gmail integration.

Harness testing is preferred for:

- receipt detection
- attachment handling
- file naming
- manifest tracking
- duplicate prevention
- runtime mode behavior

---

## Gmail Testing Rules

Do not require real Gmail access for unit tests.

Prefer:

- mocked providers
- fake message payloads
- local sample files
- fake attachments

Real Gmail access should remain optional and manual.

---

## File System Testing Rules

Tests that write files should:

- use temporary directories
- avoid modifying real receipt folders
- avoid overwriting files
- clean up after themselves when appropriate

---

## Test Data Rules

Use fake and sanitized data only.

Do not include:

- real donation receipts
- real personal emails
- real OAuth tokens
- real account names
- real filesystem paths

---

## Required Test Coverage Areas

Important project areas should include tests for:

- donation detection
- duplicate handling
- filename generation
- date parsing
- manifest tracking
- dry-run behavior
- runtime mode selection

---

## Failure Scenario Testing

Tests should include negative and recovery scenarios when possible.

Possible examples:

- missing attachment
- unsupported file extension
- duplicate filename collision
- malformed email payload
- corrupted manifest file

---

## AI Collaboration Rules

Claude should add or update tests together with meaningful business logic changes.

Claude should avoid introducing untested critical behavior.

Claude should explain if a behavior is difficult to test safely.

Claude should prefer simple and readable tests over overly abstract test frameworks.

---

## Definition Of Good Tests

Good tests in this project should be:

- easy to understand
- deterministic
- isolated
- local
- fast
- maintainable