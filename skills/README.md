# Skills

Directory for storing AI assistant skills for **Antigravity (`agy`)** and **Claude Code (`claudecode`)**.

## Directory Structure

```text
skills/
├── agy/          # Antigravity-specific skills
├── claudecode/   # Claude Code-specific skills / commands
└── shared/       # Cross-compatible skills (standard Agent Skills format)
```

## Skill Format

Each skill should reside in its own folder containing a `SKILL.md` file:

```text
skills/<category>/<skill-name>/
├── SKILL.md          # Required: main instructions with YAML frontmatter
├── scripts/          # Optional: helper scripts
├── references/       # Optional: additional documentation
└── resources/        # Optional: templates or assets
```

### Example `SKILL.md`

```markdown
---
name: my-skill
description: Use this skill when the user wants to accomplish XYZ.
---

# My Skill

Step-by-step instructions for the agent...
```

## Global Integration

- **Antigravity (`agy`)**:
  - Symlink to `~/.gemini/config/skills/<skill-name>`
  - Or register in `~/.gemini/config/skills.json`:
    ```json
    {
      "entries": [
        { "path": "~/workspace/dotfiles/skills/agy" },
        { "path": "~/workspace/dotfiles/skills/shared" }
      ]
    }
    ```
- **Claude Code**:
  - Symlink to `~/.claude/skills/<skill-name>` or `~/.claude/commands/`
