# ADR 001: IronAgent Hook Integration Pattern

## Status

Accepted

## Decision

IronAgent will integrate with IronAgent through a minimal hook seam rather than a deep fork of core
business logic.

## Consequences

- lower upgrade cost
- clearer ownership between execution and decision planes
- explicit failure modes for hook timeout and service unavailability
