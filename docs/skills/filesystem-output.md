# Skill: Filesystem Output

## Purpose

Define how the project should safely create folders, generate filenames, and write receipt files to the local filesystem.

The project should prioritize safety, determinism, and recoverability.

---

## Core Principles

- Never overwrite existing files
- Prefer append-only behavior
- Prefer deterministic naming
- Keep folder structure predictable
- Keep writes reversible when possible

---

## Folder Structure Rules

Receipt files should be organized by:

- account
- year
- month

Preferred structure:

```text
receipts/<account>/<year>/<month>/