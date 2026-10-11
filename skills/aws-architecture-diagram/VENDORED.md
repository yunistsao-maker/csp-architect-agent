# Vendored skill

- Source: https://github.com/awslabs/agent-plugins/tree/fbefb37/plugins/deploy-on-aws/skills/aws-architecture-diagram
- Upstream commit: `fbefb37` (2026-10-09). License: Apache-2.0 (see `LICENSE`).

## Local changes
1. `references/group-styles.md`: `grIcon=mxgraph.aws4.group_public_subnet` and `group_private_subnet` replaced by
   `group_security_group`. draw.io has no stencils with the first two names, so the subnet icon rendered blank.
   draw.io's own sidebar draws public/private subnets as `group_security_group` with the subnet colors.
   Upstream's `validate_drawio.py` only checks `shape=` and `resIcon=`, so it does not catch `grIcon` names.
2. Removed the 7 example `.drawio` files (about 285 KB). `SKILL.md` already tells the model not to open them.

Claude Code specifics in `SKILL.md` (hooks, `./docs/`, `${PLUGIN_ROOT}` scripts, asking questions) are overridden at
runtime by `prompts/skills_runtime.md`, so this file stays identical to upstream and is easy to re-sync.
