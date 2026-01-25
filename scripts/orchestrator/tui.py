"""Terminal UI for Clover using Rich Live display."""

from __future__ import annotations

import sys
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .agent_context import AgentContext, AgentRegistry

if TYPE_CHECKING:
    from .config import Config


class CloverDisplay:
    """Rich Live display for Clover activity."""

    def __init__(self, config: "Config"):
        """Initialize the display.

        Args:
            config: Clover configuration.
        """
        self.config = config
        self.registry = AgentRegistry()
        self.system_log: deque[str] = deque(maxlen=10)
        self.console = Console()
        self._live: Live | None = None

    def log(self, message: str) -> None:
        """Add a message to the system log.

        Args:
            message: Message to log.
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.system_log.append(f"[dim]{timestamp}[/dim] {message}")

    def create_agent(
        self,
        work_type: str,
        number: int,
        title: str,
        branch_name: str | None = None,
    ) -> AgentContext:
        """Create and register a new agent.

        Args:
            work_type: Type of work ("issue" or "pr_review").
            number: Issue or PR number.
            title: Issue or PR title.
            branch_name: Optional branch name.

        Returns:
            The created agent context.
        """
        agent = self.registry.create_agent(
            work_type=work_type,
            number=number,
            title=title,
            branch_name=branch_name,
        )
        self.log(f"Started {work_type} #{number}: {title[:40]}")
        return agent

    def get_output_callback(self, agent: AgentContext):
        """Get an output callback for the given agent.

        Args:
            agent: Agent context to route output to.

        Returns:
            Callback function for output.
        """
        display = self  # Capture reference to display

        def callback(line: str, tool_name: str | None = None) -> None:
            agent.add_output(line)
            if tool_name:
                agent.set_tool(tool_name)
            elif "completed" in line.lower() or "done" in line.lower():
                agent.set_tool(None)
            # Note: Don't call refresh() here - let Rich's automatic timer handle it
            # to avoid excessive refreshes (20-50+/sec during heavy output)

        return callback

    def _render_header(self) -> Panel:
        """Render the header panel."""
        status = "[green]●[/green] polling" if self._live else "[yellow]●[/yellow] starting"
        title = Text()
        title.append("CLOVER", style="bold cyan")
        title.append(f" - {self.config.github_repo}", style="dim")
        title.append("  ")
        title.append_text(Text.from_markup(status))

        return Panel(title, style="cyan", height=3)

    def _render_system_log(self) -> Panel:
        """Render the system log panel."""
        if not self.system_log:
            content = Text("No activity yet...", style="dim")
        else:
            lines = list(self.system_log)
            content = Text.from_markup("\n".join(lines))

        # Use fixed height to prevent layout jitter
        return Panel(
            content,
            title="[bold]System Log[/bold]",
            border_style="blue",
            height=12,
        )

    def _render_agent_panel(self, agent: AgentContext) -> Panel:
        """Render a single agent panel.

        Args:
            agent: Agent to render.

        Returns:
            Panel for the agent.
        """
        # Build title
        if agent.work_type == "issue":
            icon = "📝"
            title = f"{icon} Issue #{agent.number}"
        else:
            icon = "🔍"
            title = f"{icon} PR Review #{agent.number}"

        # Status indicator
        if agent.status == "running":
            status_style = "green"
            status_icon = "●"
        elif agent.status == "completed":
            status_style = "blue"
            status_icon = "✓"
        else:
            status_style = "red"
            status_icon = "✗"

        # Build subtitle line
        subtitle = Text()
        if agent.branch_name:
            subtitle.append(agent.branch_name, style="dim")
        if agent.current_tool:
            if subtitle:
                subtitle.append("  ")
            subtitle.append(f"⚡ {agent.current_tool}", style="yellow")
        elif agent.status == "running" and not agent.output_lines:
            if subtitle:
                subtitle.append("  ")
            subtitle.append("Starting...", style="dim italic")

        # Build content from output lines
        # Use fixed panel height to prevent layout jitter when status changes
        num_lines = 8
        panel_height = 12

        content = Text()
        if agent.output_lines:
            lines = list(agent.output_lines)[-num_lines:]
            content.append("\n".join(lines))
        elif agent.status == "failed":
            content.append("See error above", style="dim")

        # Build full panel content
        full_content = Group(subtitle, Text(""), content)

        return Panel(
            full_content,
            title=f"[bold]{title}[/bold] [{status_style}]{status_icon}[/{status_style}]",
            border_style=status_style,
            height=panel_height,
        )

    def _get_visible_agents(self) -> list:
        """Get agents that should be visible in the TUI.

        Filters out completed/failed agents after they've been visible for a while.
        """
        visible = []
        for agent in self.registry.get_all_agents():
            if agent.status == "running":
                visible.append(agent)
            else:
                # Keep completed/failed agents visible for 30 seconds
                elapsed = agent.seconds_since_finished()
                if elapsed is None or elapsed < 30:
                    visible.append(agent)
        return visible

    def _render_agents(self) -> Group | Panel:
        """Render the agents section."""
        agents = self._get_visible_agents()

        if not agents:
            return Panel(
                Text("No active agents. Waiting for work...", style="dim", justify="center"),
                title="[bold]Agents[/bold]",
                border_style="dim",
                height=6,
            )

        # Stack agent panels vertically using Group
        panels = [self._render_agent_panel(agent) for agent in agents]
        return Group(*panels)

    def render(self) -> Layout:
        """Render the full display layout.

        Returns:
            The rendered layout.
        """
        layout = Layout()

        # Main vertical split - pass renderables directly
        layout.split_column(
            Layout(self._render_header(), name="header", size=3),
            Layout(self._render_system_log(), name="log", size=12),
            Layout(self._render_agents(), name="agents"),
        )

        return layout

    def refresh(self) -> None:
        """Refresh the display immediately.

        With auto-refresh enabled, this is usually not needed, but can be
        called to force an immediate update.
        """
        if self._live:
            self._live.refresh()

    def start(self) -> None:
        """Start the live display."""
        self._live = Live(
            self.render,  # Pass the method, not the result, so it's re-called on each refresh
            console=self.console,
            refresh_per_second=10,
            screen=True,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None


def is_tty() -> bool:
    """Check if stdout is a TTY.

    Returns:
        True if stdout is a TTY.
    """
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
