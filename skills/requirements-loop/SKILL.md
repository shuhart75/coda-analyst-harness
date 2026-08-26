# Requirements Loop

Mode: `requirements`

Inputs: baseline/current, source materials, root feature requirements, relevant neighboring features.

Writes: root requirements, requirement state, cross-feature impact section, bounded context records, and explicit exchange revisions only on a transfer command.

Rules: the root requirements are the only authored requirement document; slices and contour packs are not created; new roots use the compact specification in `core/requirements-profile.md` and controlled wording from `core/requirements-wording.md`; delivery runs the three-level audit in `core/requirements-audit.md`; local stale tails block completion; only the user-owner approves requirements.

Validation: individual rules, cross-requirement interactions, delivery readiness, direct `REQ-*` traceability, links, stale-term sweep, Russian-language check, compact-profile check, PlantUML integrity, and explicit coverage of every cross-feature impact.
