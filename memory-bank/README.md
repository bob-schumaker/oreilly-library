# Memory Bank

This directory stores durable project context for future Cline sessions working
on `oreilly-library`.

## Core Files

- `projectbrief.md` — project goals, scope, and constraints
- `productContext.md` — why the project exists and the user-facing problems it
  solves
- `activeContext.md` — current focus, recent context, and immediate next steps
- `systemPatterns.md` — architecture, component boundaries, and design patterns
- `techContext.md` — technology stack, tools, setup, and environment notes
- `progress.md` — what works, what remains, and known follow-up tracks

## Repository-specific conventions

- Keep memory-bank updates concise and grounded in observed repository state.
- Treat `README.md`, `pyproject.toml`, and `src/oreilly_library/` as the main
  sources of truth for project behavior.
- Always include `memory-bank/notes/historical-user-prompts.txt` in commits
  that commit memory-bank changes.
- Use task-specific handoff notes under `cline-tasks/` for branch, validation,
  integration, or commit-boundary state that is too detailed for this memory
  bank.
- This repository did not contain `skills/docs/cline-memory-bank.md` at memory
  bank initialization time, so initialization followed the active installed
  memory-bank skill and the standard core-file shape.

## Usage

On resume, read `activeContext.md` and `progress.md` first. Open
`projectbrief.md`, `systemPatterns.md`, or `techContext.md` when the task needs
scope, architecture, or tooling context.
