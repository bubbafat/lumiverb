# ADR-NNN: Title

## Status

<!-- One of: Proposed | Accepted | In Progress | Done | Superseded | Deferred -->
Proposed

## Progress

<!-- Update this table as phases complete. This is the at-a-glance view. -->

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | ... | Not started |
| 2 | ... | Not started |

## Overview

<!-- 2-3 paragraphs max. What problem does this solve? Why now? What does the user experience look like before and after? -->

## Motivation

<!-- What's broken, missing, or painful? Concrete examples from real usage. Link to issues or user feedback if available. -->

## Design

<!-- The core technical decisions. Use subsections for each major area. Include:
- Data model (schema, tables, relationships)
- API surface (endpoints, request/response shapes)
- UI behavior (pages, components, interactions)
- CLI commands (if applicable)

Use code blocks for schemas, API examples, and CLI usage. Be precise enough that implementation is unambiguous. -->

### Data Model

### API Endpoints

### UI

### CLI (if applicable)

## Edge Cases

<!-- Table format preferred. Every scenario that could go wrong or behave unexpectedly. -->

| Scenario | Behavior |
|----------|----------|
| ... | ... |

## Code References

<!-- Pointers to existing code that this ADR touches, depends on, or extends.
These help anyone picking up the ADR find their way into the codebase quickly. -->

| Area | File | Notes |
|------|------|-------|
| ... | `src/...` | ... |

## Doc References

<!-- Which docs need to be read for context and updated when implementation is done. -->

- `docs/cursor-api.md` — API reference (update with new endpoints)
- `docs/cursor-cli.md` — CLI reference (update with new commands)
- `docs/architecture.md` — System design (update if architecture changes)

## Build Phases

### Requirements

Every phase must satisfy all of the following before it is marked complete:

1. **Tests**: New backend tests for every endpoint and repository method. Edge cases from the table above must be covered as they become relevant. **All tests must pass** — not just new or affected tests, the entire suite (`uv run pytest tests/`). No phase is done until the full suite is clean.
2. **Types**: Frontend TypeScript must compile cleanly (`npx tsc --noEmit`).
3. **Build**: Vite must build without errors (`npx vite build`).
4. **Documentation**: Relevant docs updated to reflect changes in the phase.
5. **Progress**: The phase status table above is updated when a phase completes.
6. **Forward compatibility**: Implementation must read ahead to future phases and ensure data model, API shapes, and component interfaces are set up correctly. If current work reveals changes needed in a future phase, update that phase's description.
7. **Backward compatibility**: If current implementation invalidates or changes assumptions in a previous or future phase, those phases must be updated in this document before the current phase is marked complete.

### Phase 1 — [Title]

<!-- What gets built, what gets tested, what gets documented.
Start with a bullet list of deliverables, then add detail as needed.
End with explicit acceptance criteria. -->

**Deliverables:**
- ...

**Does NOT include:** ...

**Read-ahead:** ...

**Done when:**
- [ ] All deliverables implemented
- [ ] Tests written and passing (`uv run pytest tests/`)
- [ ] Docs updated
- [ ] Phase status updated above

### Phase 2 — [Title]

...

## Alternatives Considered

<!-- What other approaches were evaluated and why they were rejected. Brief — one paragraph each. -->

## What This Does NOT Include

<!-- Explicit scope fence. List future features that are related but out of scope.
This prevents scope creep and sets expectations for follow-up ADRs. -->

## Open Questions

<!-- Anything unresolved that needs input before or during implementation.
Remove this section once all questions are answered. -->
