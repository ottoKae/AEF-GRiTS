# AEF-GRiTS agent entry point

For AEF download planning, local/SSH execution, progress checks, recovery or
delivery, read [.agents/skills/aef-grits-download/SKILL.md](.agents/skills/aef-grits-download/SKILL.md).
It is the single repository-owned skill; do not require another GRiTS checkout
or copy a second maintained version into a global skill directory.

Use the existing download modules and the user's selected Python environment.
The skill describes operating procedures, not additional runtime guarantees or
authorization to start a download. Keep credentials, real Project IDs, SSH
identities and job-specific paths in private runtime configuration, not Git.

The skill does not apply to unrelated GUI, manuscript or model changes.
