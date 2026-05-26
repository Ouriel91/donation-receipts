# CLAUDE WORKFLOW

## Purpose

This file defines how to work with Claude safely and effectively in this project.

The goal is to keep AI-assisted development structured, small, testable, and reversible.

---

## Core Principle

Plan first, patch small, test always, commit only clean.

---

## Before Starting A Task

Before asking Claude to implement anything:

- Make sure you are on a dedicated branch
- Make sure the working tree is clean
- Read the relevant project docs
- Define a narrow task
- Define what is out of scope
- Define expected files to change
- Define required tests

---

## When To Use Plan Mode

Use Plan Mode for:

- New features
- Runtime flow changes
- File writing behavior
- Gmail integration
- Manifest changes
- Refactors
- Anything touching multiple files

Plan Mode should produce:

- Summary of the intended change
- Files expected to change
- Risks
- Test plan
- Confirmation before implementation

Do not use Plan Mode for tiny typo fixes or small documentation edits.

---

## How To Prompt Claude

A good task prompt should include:

- Goal
- In scope
- Out of scope
- Primary files expected to change
- Testing requirements
- Definition of done

Use `docs/TASK_TEMPLATE.md` as the default structure.

---

## What Not To Ask Claude

Avoid vague prompts such as:

- "Build this feature"
- "Improve the project"
- "Refactor everything"
- "Make it production ready"
- "Add whatever is needed"

These prompts are too open-ended and may cause overengineering.

---

## Required Context

For most implementation tasks, ask Claude to read:

- `README.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/RULES.md`
- relevant files in `docs/skills/`
- the specific source files involved in the task

Do not paste secrets or real personal data into Claude.

---

## Conversation Management

Use the current conversation when:

- The task is small
- Context is still fresh
- The working branch has not changed significantly

Start a new conversation when:

- A task is completed and merged
- The conversation becomes too long
- Claude starts making incorrect assumptions
- The project structure changed significantly
- You are starting a new major feature

When starting a new conversation, provide a short summary:

- Current branch
- Goal of the task
- Relevant docs to read
- Current project status

---

## Init / Project Scan

Run an init or project scan when:

- Starting work with Claude for the first time
- Opening the project on a new machine
- After major structural changes
- When Claude seems unaware of the current files

Do not repeatedly init for every tiny task.

---

## Implementation Rules

Claude should:

- Make small focused changes
- Avoid unrelated refactors
- Avoid unnecessary abstractions
- Add or update tests with meaningful logic changes
- Keep Gmail integration isolated
- Keep filesystem writes safe
- Preserve dry-run behavior
- Avoid hardcoded secrets or personal paths

---

## Review Rules

After Claude changes code:

- Read the diff
- Check unexpected file changes
- Run tests
- Run relevant harness commands
- Verify no secrets were added
- Verify `.gitignore` still protects local files

---

## If Claude Overreaches

If Claude changes too much, stop and say:

```text
This is too broad. Revert to a smaller minimal patch.
Only change the files required for the current task.
Do not refactor unrelated code.