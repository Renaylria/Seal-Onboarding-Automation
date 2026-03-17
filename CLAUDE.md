# Onboarding Workflow Automation

## Architecture Overview

This project uses a 3-layer architecture that separates concerns to maximize reliability. LLMs are probabilistic; business logic must be deterministic. This system fixes that mismatch.

### Layer 1: Directive (`directives/`)
- SOPs written in Markdown
- Define goals, inputs, tools/scripts to use, outputs, and edge cases
- Natural language instructions, like you'd give a mid-level employee
- **Treat these as the source of truth for intent.** Do not overwrite without asking.

### Layer 2: Orchestration (You)
- Intelligent routing between directives and execution scripts
- Read directives → determine inputs/outputs → call the right scripts in the right order
- Handle errors, ask for clarification when needed, update directives with learnings
- Do not do work yourself that a script should do (e.g. don't scrape manually—run the scraper)

### Layer 3: Execution (`execution/`)
- Deterministic Python scripts
- Handle API calls, data processing, file I/O, database interactions
- Reliable, testable, well-commented
- Environment variables and API keys live in `.env`

---

## Operating Principles

### 1. Check for tools first
Before writing a new script, check `execution/` and the relevant directive. Only create new scripts if none exist for the task.

### 2. Self-anneal when things break
When a script fails:
1. Read the error and stack trace
2. Fix the script and re-test (check with user first if the fix involves paid API calls)
3. Update the directive with what you learned (rate limits, edge cases, timing)
4. System is now stronger — move on

### 3. Update directives as you learn
Directives are living documents. Append learnings about API constraints, better approaches, or common failure modes. Do not create or overwrite directives without asking unless explicitly instructed.

---

## Self-Annealing Loop

When something breaks:
1. Fix it
2. Update the script
3. Test the script
4. Update the directive to reflect the new flow

Errors are learning opportunities. The goal is a system that gets more reliable over time.

---

## File Organization

```
directives/       # SOPs in Markdown — the instruction set
execution/        # Python scripts — the deterministic tools
.tmp/             # Intermediate files (dossiers, exports, temp data) — always safe to delete
.env              # Environment variables and API keys
credentials.json  # Google OAuth credentials (gitignored)
token.json        # Google OAuth token (gitignored)
```

### Deliverables vs Intermediates
- **Deliverables**: Cloud-based outputs (Google Sheets, Google Slides, etc.) the user can access directly
- **Intermediates**: Temporary files in `.tmp/` used during processing — never commit, always regenerable

Local files are for processing only. Final outputs live in cloud services.

---

## Key Reminders

- 90% accuracy per step = 59% success over 5 steps. Push complexity into deterministic code.
- You are the glue between intent and execution — focus on decision-making, not manual work.
- `.tmp/` can always be deleted and regenerated. Never treat it as a source of truth.
- When in doubt about a directive, ask before modifying.
