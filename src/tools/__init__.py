"""ALL_TOOLS registry — central lookup for every tool."""

from src.types import ToolDef
from src.tools.bash import tool as bash_tool
from src.tools.read_file import tool as read_file_tool
from src.tools.grep import tool as grep_tool
from src.tools.skill import tool as skill_tool
from src.tools.agent import tool as agent_tool

ALL_TOOLS: dict[str, ToolDef] = {
    bash_tool.name: bash_tool,
    read_file_tool.name: read_file_tool,
    grep_tool.name: grep_tool,
    skill_tool.name: skill_tool,
    agent_tool.name: agent_tool,
}
