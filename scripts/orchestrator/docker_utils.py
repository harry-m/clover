"""Docker utilities for Clover test sessions."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional


class DockerError(Exception):
    """Error from Docker operations."""

    pass


async def run_command(
    cmd: list[str],
    cwd: Optional[Path] = None,
    capture_output: bool = True,
) -> tuple[int, str, str]:
    """Run a command asynchronously.

    Args:
        cmd: Command and arguments to run.
        cwd: Working directory for the command.
        capture_output: Whether to capture stdout/stderr.

    Returns:
        Tuple of (return_code, stdout, stderr).
    """
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE if capture_output else None,
        stderr=asyncio.subprocess.PIPE if capture_output else None,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode() if stdout else "",
        stderr.decode() if stderr else "",
    )


def find_docker_compose() -> str:
    """Find the docker compose command.

    Returns:
        Command to use for docker compose ('docker compose' or 'docker-compose').

    Raises:
        DockerError: If docker compose is not found.
    """
    # Try 'docker compose' first (Docker Compose V2)
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return "docker compose"

    # Fall back to 'docker-compose' (Docker Compose V1)
    if shutil.which("docker-compose"):
        return "docker-compose"

    raise DockerError(
        "Docker Compose not found. Install Docker Desktop or docker-compose."
    )


class DockerCompose:
    """Wrapper for docker compose commands."""

    def __init__(self, compose_file: Path, project_name: str):
        """Initialize the Docker Compose wrapper.

        Args:
            compose_file: Path to docker-compose.yml file.
            project_name: Project name for container naming.
        """
        self.compose_file = compose_file
        self.project_name = project_name
        self._compose_cmd = find_docker_compose()

    def _build_cmd(self, *args: str) -> list[str]:
        """Build a docker compose command.

        Args:
            args: Additional arguments for docker compose.

        Returns:
            Full command as list.
        """
        if self._compose_cmd == "docker compose":
            base = ["docker", "compose"]
        else:
            base = ["docker-compose"]

        return [
            *base,
            "-f", str(self.compose_file),
            "-p", self.project_name,
            *args,
        ]

    async def up(
        self,
        detach: bool = True,
        volumes: Optional[list[str]] = None,
    ) -> tuple[int, str, str]:
        """Start containers.

        Args:
            detach: Run in detached mode.
            volumes: Additional volume mounts (format: "host:container:mode").

        Returns:
            Tuple of (return_code, stdout, stderr).
        """
        cmd = self._build_cmd("up", "--build")
        if detach:
            cmd.append("-d")

        # Note: docker compose doesn't support -v flag on 'up', but we can
        # use environment variables or override files. For simplicity, we'll
        # document that Claude should be mounted via docker-compose.yml or
        # installed in the container.
        #
        # A better approach for Claude injection is to use docker run or
        # exec with the CLI path mounted.
        return await run_command(cmd, cwd=self.compose_file.parent)

    async def down(self, volumes: bool = False) -> tuple[int, str, str]:
        """Stop and remove containers.

        Args:
            volumes: Also remove volumes.

        Returns:
            Tuple of (return_code, stdout, stderr).
        """
        cmd = self._build_cmd("down")
        if volumes:
            cmd.append("-v")
        return await run_command(cmd, cwd=self.compose_file.parent)

    async def ps(self) -> list[dict]:
        """List running containers.

        Returns:
            List of container info dicts with 'name', 'service', 'status' keys.
        """
        cmd = self._build_cmd("ps", "--format", "json")
        returncode, stdout, _ = await run_command(cmd, cwd=self.compose_file.parent)

        if returncode != 0:
            return []

        import json
        containers = []
        for line in stdout.strip().split("\n"):
            if line:
                try:
                    data = json.loads(line)
                    containers.append({
                        "name": data.get("Name", ""),
                        "service": data.get("Service", ""),
                        "status": data.get("State", ""),
                    })
                except json.JSONDecodeError:
                    pass

        return containers

    async def logs(
        self,
        service: Optional[str] = None,
        follow: bool = False,
        tail: Optional[int] = None,
    ) -> asyncio.subprocess.Process:
        """Get container logs.

        Args:
            service: Specific service to get logs from.
            follow: Follow log output.
            tail: Number of lines to show from end.

        Returns:
            Subprocess for reading logs.
        """
        cmd = self._build_cmd("logs")
        if follow:
            cmd.append("-f")
        if tail is not None:
            cmd.extend(["--tail", str(tail)])
        if service:
            cmd.append(service)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.compose_file.parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return process

    async def exec(
        self,
        service: str,
        command: list[str],
        interactive: bool = False,
        tty: bool = False,
    ) -> tuple[int, str, str]:
        """Execute a command in a running container.

        Args:
            service: Service name to exec into.
            command: Command to run.
            interactive: Keep stdin open.
            tty: Allocate a pseudo-TTY.

        Returns:
            Tuple of (return_code, stdout, stderr).
        """
        cmd = self._build_cmd("exec")
        if not interactive:
            cmd.append("-T")  # Disable pseudo-tty allocation
        if tty and interactive:
            cmd.append("-it")
        cmd.append(service)
        cmd.extend(command)

        return await run_command(cmd, cwd=self.compose_file.parent)

    async def get_container_name(self, service: str) -> Optional[str]:
        """Get the container name for a service.

        Args:
            service: Service name.

        Returns:
            Container name if found, None otherwise.
        """
        containers = await self.ps()
        for container in containers:
            if container["service"] == service:
                return container["name"]
        return None

    async def get_services(self) -> list[str]:
        """Get list of services defined in compose file.

        Returns:
            List of service names.
        """
        cmd = self._build_cmd("config", "--services")
        returncode, stdout, _ = await run_command(cmd, cwd=self.compose_file.parent)

        if returncode != 0:
            return []

        return [s.strip() for s in stdout.strip().split("\n") if s.strip()]

    async def port(self, service: str, container_port: int) -> Optional[int]:
        """Get the host port mapped to a container port.

        Args:
            service: Service name.
            container_port: Container port number.

        Returns:
            Host port number if mapped, None otherwise.
        """
        cmd = self._build_cmd("port", service, str(container_port))
        returncode, stdout, _ = await run_command(cmd, cwd=self.compose_file.parent)

        if returncode != 0 or not stdout.strip():
            return None

        # Output format is "0.0.0.0:PORT" or ":::PORT"
        try:
            return int(stdout.strip().split(":")[-1])
        except (ValueError, IndexError):
            return None


class PortManager:
    """Manages dynamic port allocation for Docker Compose."""

    def __init__(self, compose_file: Path):
        """Initialize the port manager.

        Args:
            compose_file: Path to docker-compose.yml file.
        """
        self.compose_file = compose_file
        self._original_ports: dict[str, list[dict]] = {}

    def _load_compose(self) -> dict:
        """Load the docker-compose.yml file.

        Returns:
            Parsed YAML data.
        """
        import yaml
        with open(self.compose_file) as f:
            return yaml.safe_load(f) or {}

    def _save_compose(self, data: dict, path: Path) -> None:
        """Save compose data to a file.

        Args:
            data: Compose data to save.
            path: Path to save to.
        """
        import yaml
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    def get_port_mappings(self) -> dict[str, list[dict]]:
        """Get port mappings from the compose file.

        Returns:
            Dict mapping service names to their port configurations.
            Each port config has 'container' and optionally 'host' keys.
        """
        data = self._load_compose()
        services = data.get("services", {})
        mappings = {}

        for service_name, service_config in services.items():
            ports = service_config.get("ports", [])
            port_list = []

            for port in ports:
                if isinstance(port, int):
                    # Just a container port
                    port_list.append({"container": port, "host": None})
                elif isinstance(port, str):
                    # Parse "host:container" or "container"
                    parts = port.split(":")
                    if len(parts) == 1:
                        port_list.append({"container": int(parts[0]), "host": None})
                    elif len(parts) == 2:
                        host_port = int(parts[0]) if parts[0] else None
                        port_list.append({
                            "container": int(parts[1]),
                            "host": host_port,
                        })
                    else:
                        # IP:host:container format
                        port_list.append({
                            "container": int(parts[2]),
                            "host": int(parts[1]) if parts[1] else None,
                        })
                elif isinstance(port, dict):
                    # Long format
                    port_list.append({
                        "container": port.get("target"),
                        "host": port.get("published"),
                    })

            if port_list:
                mappings[service_name] = port_list

        return mappings

    def create_dynamic_ports_file(self) -> Path:
        """Create a compose override file with dynamic host ports.

        Returns:
            Path to the override file.
        """
        data = self._load_compose()
        services = data.get("services", {})

        # Store original ports for later reference
        self._original_ports = self.get_port_mappings()

        # Convert all ports to dynamic (no host port specified)
        for service_name, service_config in services.items():
            ports = service_config.get("ports", [])
            new_ports = []

            for port in ports:
                if isinstance(port, int):
                    new_ports.append(str(port))
                elif isinstance(port, str):
                    parts = port.split(":")
                    if len(parts) >= 2:
                        # Use just the container port
                        container_port = parts[-1]
                        new_ports.append(container_port)
                    else:
                        new_ports.append(port)
                elif isinstance(port, dict):
                    # Convert to string format with just container port
                    container_port = port.get("target")
                    if container_port:
                        new_ports.append(str(container_port))

            if new_ports:
                service_config["ports"] = new_ports

        # Save to override file
        override_file = self.compose_file.parent / ".clover-compose-override.yml"
        self._save_compose(data, override_file)
        return override_file

    def get_expected_ports(self) -> dict[str, list[int]]:
        """Get the expected container ports that will need mapping.

        Returns:
            Dict mapping service names to list of container ports.
        """
        mappings = self.get_port_mappings()
        result = {}
        for service, ports in mappings.items():
            result[service] = [p["container"] for p in ports if p["container"]]
        return result


def get_claude_cli_path() -> Optional[Path]:
    """Find the Claude CLI executable path.

    Returns:
        Path to claude CLI if found, None otherwise.
    """
    claude_path = shutil.which("claude")
    if claude_path:
        return Path(claude_path)
    return None


def get_claude_config_dir() -> Path:
    """Get the Claude configuration directory.

    Returns:
        Path to ~/.claude directory.
    """
    return Path.home() / ".claude"


async def copy_to_container(
    container_name: str,
    src: Path,
    dest: str,
) -> tuple[int, str, str]:
    """Copy a file or directory into a container.

    Args:
        container_name: Name of the container.
        src: Source path on host.
        dest: Destination path in container.

    Returns:
        Tuple of (return_code, stdout, stderr).
    """
    cmd = ["docker", "cp", str(src), f"{container_name}:{dest}"]
    return await run_command(cmd)


async def inject_claude_into_container(container_name: str) -> bool:
    """Inject Claude CLI and config into a running container.

    Args:
        container_name: Name of the container.

    Returns:
        True if injection succeeded, False otherwise.
    """
    import logging
    logger = logging.getLogger(__name__)

    claude_path = get_claude_cli_path()
    if not claude_path:
        logger.warning("Claude CLI not found on host - skipping injection")
        return False

    # Copy Claude CLI
    returncode, _, stderr = await copy_to_container(
        container_name,
        claude_path,
        "/usr/local/bin/claude",
    )
    if returncode != 0:
        logger.warning(f"Failed to copy Claude CLI: {stderr}")
        return False

    # Copy Claude config directory if it exists
    claude_config = get_claude_config_dir()
    if claude_config.exists():
        # First ensure /root exists
        await run_command(["docker", "exec", container_name, "mkdir", "-p", "/root"])

        returncode, _, stderr = await copy_to_container(
            container_name,
            claude_config,
            "/root/.claude",
        )
        if returncode != 0:
            logger.warning(f"Failed to copy Claude config: {stderr}")
            # Continue anyway - Claude may still work

    logger.info(f"Injected Claude CLI into container {container_name}")
    return True


async def exec_interactive(
    container_name: str,
    command: list[str] = None,
    workdir: Optional[str] = None,
) -> None:
    """Execute an interactive command in a container.

    This replaces the current process with docker exec.

    Args:
        container_name: Name of the container.
        command: Command to run (default: bash).
        workdir: Working directory in container.
    """
    import os

    cmd = ["docker", "exec", "-it"]
    if workdir:
        cmd.extend(["-w", workdir])
    cmd.append(container_name)
    cmd.extend(command or ["bash"])

    # Replace current process with docker exec
    os.execvp("docker", cmd)
