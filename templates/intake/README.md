# Feature Intake Templates

This directory contains the canonical preflight template for starting a new feature safely.

## Purpose

Use feature intake before creating a new `features/<slug>/` folder when the user points to an external folder, a change request bundle, or a loosely defined initiative and says that this is a new feature.

The intake step exists to prevent premature scaffolding.

## What the LLM must do during intake

- inspect the source folder or documents;
- compare the candidate feature against `baseline/current/`;
- compare it against existing `features/*`;
- compare it against legacy planning and source materials when relevant;
- separate existing coverage from the true new delta;
- propose feature slug, slices, planning scope and affected artifacts;
- list baseline gaps and workflow gaps before feature creation.

## What the LLM must not do during intake

- do not scaffold `features/<slug>/` yet;
- do not create slices yet;
- do not write requirement packs yet;
- do not rewrite baseline/current yet unless the user explicitly asks for a baseline readiness pass.

## Output location

Store intake results in `planning/intake/<candidate-slug>.md`.

If the slug is still uncertain, use a temporary descriptive name and update it later.
