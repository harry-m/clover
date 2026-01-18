"""Agent context tracking for TUI display."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class AgentContext:
    """Context for a single running agent."""

    agent_id: str
    work_type: str  # "issue" or "pr_review"
    number: int
    title: str
    branch_name: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    status: str = "running"  # "running", "completed", "failed"
    current_tool: Optional[str] = None

    def add_output(self, line: str) -> None:
        """Add a line of output to the buffer."""
        self.output_lines.append(line)

    def set_tool(self, tool_name: Optional[str]) -> None:
        """Set the currently executing tool."""
        self.current_tool = tool_name

    def mark_completed(self) -> None:
        """Mark the agent as completed."""
        self.status = "completed"
        self.finished_at = datetime.utcnow()

    def mark_failed(self) -> None:
        """Mark the agent as failed."""
        self.status = "failed"
        self.finished_at = datetime.utcnow()

    def seconds_since_finished(self) -> Optional[float]:
        """Return seconds since the agent finished, or None if still running."""
        if self.finished_at is None:
            return None
        return (datetime.utcnow() - self.finished_at).total_seconds()


class AgentRegistry:
    """Registry for tracking active agents."""

    def __init__(self):
        self.agents: dict[str, AgentContext] = {}
        self._next_id: int = 1

    def create_agent(
        self,
        work_type: str,
        number: int,
        title: str,
        branch_name: Optional[str] = None,
    ) -> AgentContext:
        """Create and register a new agent context."""
        agent_id = f"agent-{self._next_id}"
        self._next_id += 1

        context = AgentContext(
            agent_id=agent_id,
            work_type=work_type,
            number=number,
            title=title,
            branch_name=branch_name,
        )
        self.agents[agent_id] = context
        return context

    def get_agent(self, agent_id: str) -> Optional[AgentContext]:
        """Get an agent by ID."""
        return self.agents.get(agent_id)

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the registry."""
        if agent_id in self.agents:
            del self.agents[agent_id]

    def get_active_agents(self) -> list[AgentContext]:
        """Get all agents with status 'running'."""
        return [a for a in self.agents.values() if a.status == "running"]

    def get_all_agents(self) -> list[AgentContext]:
        """Get all agents."""
        return list(self.agents.values())
