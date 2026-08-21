"""Skill management tool: create, update, delete skills.

Used by Dream to auto-generate skills from observed patterns.
Provides a structured interface for skill lifecycle management.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from jenny.agent.tools.base import Tool
from jenny.agent.tools.file_state import FileStates, current_file_states
from jenny.agent.tools.schema import StringSchema, tool_parameters_schema
from jenny.security.workspace_access import current_tool_workspace
from jenny.utils.path import atomic_write


class SkillManageTool(Tool):
    """Tool for managing skills: create, update, delete."""

    @property
    def name(self) -> str:
        return "skill_manage"

    @property
    def description(self) -> str:
        return (
            "Manage agent skills (procedural memory). Create, update, or delete "
            "SKILL.md files in the workspace skills directory. Use this to save "
            "repeatable workflows the agent has learned from experience."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return tool_parameters_schema(
            action=StringSchema(
                description=(
                    "Action to perform: 'create' for new skill, 'update' to modify "
                    "existing, 'delete' to remove, 'list' to show all skills"
                ),
                enum=["create", "update", "delete", "list"],
            ),
            name=StringSchema(
                description=(
                    "Skill name (lowercase, hyphens as separators). "
                    "Used as directory name under skills/"
                ),
            ),
            content=StringSchema(
                description=(
                    "Full SKILL.md content for create/update actions. "
                    "Must include YAML frontmatter with name and description."
                ),
            ),
        )

    def __init__(
        self,
        workspace: Path | None = None,
        file_states: FileStates | None = None,
    ):
        self._workspace = workspace
        self._file_states = file_states

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "list")
        name = kwargs.get("name", "").strip()
        content = kwargs.get("content", "")

        workspace = self._workspace or current_tool_workspace()
        if workspace is None:
            return "Error: no workspace available"

        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        if action == "list":
            return self._list_skills(skills_dir)
        elif action == "create":
            return self._create_skill(skills_dir, name, content)
        elif action == "update":
            return self._update_skill(skills_dir, name, content)
        elif action == "delete":
            return self._delete_skill(skills_dir, name)
        else:
            return f"Error: unknown action '{action}'"

    def _list_skills(self, skills_dir: Path) -> str:
        """List all installed skills."""
        skills = []
        if skills_dir.exists():
            for skill_dir in sorted(skills_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    # Extract description from frontmatter
                    description = self._extract_description(skill_file)
                    skills.append(f"- {skill_dir.name}: {description}")

        if not skills:
            return "No skills installed."
        return "Installed skills:\n" + "\n".join(skills)

    def _create_skill(self, skills_dir: Path, name: str, content: str) -> str:
        """Create a new skill."""
        if not name:
            return "Error: skill name is required"
        if not content:
            return "Error: skill content is required"

        # Validate name
        if not all(c.isalnum() or c == "-" for c in name) or not name:
            return "Error: skill name must be lowercase alphanumeric with hyphens"

        skill_dir = skills_dir / name
        skill_file = skill_dir / "SKILL.md"

        if skill_file.exists():
            return f"Error: skill '{name}' already exists. Use 'update' to modify it."

        # Create skill directory and file
        skill_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(skill_file, content)

        # Track file state
        file_states = self._file_states or current_file_states()
        if file_states:
            file_states.track_write(str(skill_file))

        logger.info("Skill '{}' created", name)
        return f"Skill '{name}' created successfully."

    def _update_skill(self, skills_dir: Path, name: str, content: str) -> str:
        """Update an existing skill."""
        if not name:
            return "Error: skill name is required"
        if not content:
            return "Error: skill content is required"

        skill_file = skills_dir / name / "SKILL.md"
        if not skill_file.exists():
            return f"Error: skill '{name}' does not exist. Use 'create' to make a new one."

        # Update the skill file
        atomic_write(skill_file, content)

        # Track file state
        file_states = self._file_states or current_file_states()
        if file_states:
            file_states.track_write(str(skill_file))

        logger.info("Skill '{}' updated", name)
        return f"Skill '{name}' updated successfully."

    def _delete_skill(self, skills_dir: Path, name: str) -> str:
        """Delete a skill."""
        if not name:
            return "Error: skill name is required"

        skill_dir = skills_dir / name
        if not skill_dir.exists():
            return f"Error: skill '{name}' does not exist"

        # Remove the skill directory
        import shutil
        shutil.rmtree(skill_dir)

        logger.info("Skill '{}' deleted", name)
        return f"Skill '{name}' deleted successfully."

    def _extract_description(self, skill_file: Path) -> str:
        """Extract description from SKILL.md frontmatter."""
        try:
            content = skill_file.read_text(encoding="utf-8")
            # Simple frontmatter parsing
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    frontmatter = content[3:end].strip()
                    for line in frontmatter.split("\n"):
                        if line.strip().startswith("description:"):
                            desc = line.split(":", 1)[1].strip()
                            # Remove quotes if present
                            desc = desc.strip("'\"")
                            # Truncate if too long
                            if len(desc) > 100:
                                desc = desc[:97] + "..."
                            return desc
        except Exception:
            pass
        return "(no description)"


TOOLS = [SkillManageTool]
