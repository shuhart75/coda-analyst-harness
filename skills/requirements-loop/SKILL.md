# Requirements Loop

Mode: `requirements`

Inputs: baseline/current, source materials, root feature requirements, relevant neighboring features.

Writes: root requirements, requirement state, cross-feature impact section, bounded context records, and explicit exchange revisions only on a transfer command.

Rules: the root requirements are the only authored requirement document; slices and contour packs are not created; new root documents follow `core/requirements-profile.md`; local stale tails block completion; only the user-owner approves requirements.

Validation: context, direct `REQ-*` traceability, links, stale-term sweep, Russian-language check, requirements-profile check, PlantUML integrity, and explicit coverage of every cross-feature impact.
