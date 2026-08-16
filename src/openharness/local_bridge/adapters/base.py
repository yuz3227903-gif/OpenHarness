"""The contract every local Agent implementation satisfies.

Provider-specific knowledge lives in an adapter and nowhere else. Nothing
outside this package should branch on which Agent it is talking to; callers ask
the registry for adapters and use this interface.
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from openharness.local_bridge.events import AgentEvent

DetectionStatus = Literal["available", "not_installed", "error"]

#: How long a version probe may take. A missing binary fails instantly; a slow
#: one is more likely wedged than genuinely working, and the detect endpoint has
#: to stay responsive because the UI blocks on it.
DETECT_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class AgentCapabilities:
    """What one Agent can actually do.

    Agents differ: some edit files, some only talk. The UI reads this rather
    than assuming a feature exists, so an Agent never advertises something it
    cannot deliver.
    """

    chat: bool = True
    streaming: bool = False
    shell: bool = False
    file_read: bool = False
    file_write: bool = False
    diff: bool = False
    mcp: bool = False
    skills: bool = False
    resume_session: bool = False
    approval: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class DetectionResult:
    """The outcome of looking for one Agent on this machine."""

    provider: str
    display_name: str
    installed: bool
    status: DetectionStatus
    version: str | None = None
    executable: str | None = None
    detail: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalAgentAdapter(ABC):
    """One locally installed Agent.

    Subclasses declare how to find the binary and how to read its version.
    Everything shared — running the probe safely, classifying failures — stays
    here so a new Agent is a small, declarative addition.
    """

    #: Stable identifier stored on the Agent record. Never shown to the user.
    provider: str = ""
    #: Shown in the UI.
    display_name: str = ""
    #: Executable names to look for, in order of preference.
    executables: tuple[str, ...] = ()
    #: Arguments that make the executable print its version.
    version_args: tuple[str, ...] = ("--version",)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Catch an incomplete adapter at import time rather than when a user
        # first clicks detect.
        if not getattr(cls, "__abstractmethods__", None):
            for required in ("provider", "display_name", "executables"):
                if not getattr(cls, required, None):
                    raise TypeError(f"{cls.__name__} must define {required!r}")

    @abstractmethod
    def capabilities(self) -> AgentCapabilities:
        """Declare what this Agent supports."""

    # ------------------------------------------------------------- conversation

    def build_command(self, *, prompt: str, workspace: str, session_ref: str | None) -> list[str]:
        """The argv that runs one turn.

        Returned as a list and executed without a shell, so neither the prompt
        nor the workspace can be interpreted as a command. ``session_ref`` is
        whatever :meth:`session_reference` returned earlier, letting a CLI that
        supports it continue its own conversation.
        """

        executable = self.find_executable()
        if executable is None:
            raise FileNotFoundError(f"{self.display_name} 未安装或不在 PATH 中")
        return [executable, *self.run_args(session_ref=session_ref), prompt]

    def run_args(self, *, session_ref: str | None) -> list[str]:
        """Arguments between the executable and the prompt."""

        return []

    def translate(self, line: str, session_id: str) -> list[AgentEvent]:
        """Turn one line of Agent output into protocol events.

        The default treats output as assistant text, which is right for a CLI
        that simply prints its answer. An Agent with a structured protocol
        overrides this and emits tool, command and file events instead.
        """

        from openharness.local_bridge.events import message_delta

        if not line:
            return []
        return [message_delta(session_id, line)]

    def session_reference(self, events: list[AgentEvent]) -> str | None:
        """Extract the CLI's own session id from a completed turn, if any.

        Storing it is what lets :meth:`build_command` resume rather than start
        over. Returning ``None`` means this Agent has no resumable session.
        """

        return None

    def parse_version(self, output: str) -> str | None:
        """Pull a version out of the probe output.

        The default takes the first non-empty line, which covers CLIs that
        print exactly their version. Override when a CLI is chattier.
        """

        for line in output.splitlines():
            cleaned = line.strip()
            if cleaned:
                return cleaned[:120]
        return None

    def find_executable(self) -> str | None:
        """Return the first of this Agent's executables present on PATH."""

        for name in self.executables:
            found = shutil.which(name)
            if found:
                return found
        return None

    def detect(self) -> DetectionResult:
        """Look for this Agent and report honestly what was found.

        A probe failure is reported as a failure with its reason; it is never
        rounded up to "installed" or silently swallowed.
        """

        executable = self.find_executable()
        if executable is None:
            return DetectionResult(
                provider=self.provider,
                display_name=self.display_name,
                installed=False,
                status="not_installed",
                detail=f"未在 PATH 中找到 {' / '.join(self.executables)}",
                capabilities=self.capabilities().to_dict(),
            )

        try:
            # An argv list, never a shell string, so the executable path can
            # never be interpreted as a command. A non-zero exit is a result to
            # report, not an exception, hence check=False.
            completed = subprocess.run(
                [executable, *self.version_args],
                capture_output=True,
                text=True,
                timeout=DETECT_TIMEOUT_SECONDS,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return DetectionResult(
                provider=self.provider, display_name=self.display_name,
                installed=True, status="error", executable=executable,
                detail=f"版本探测超过 {DETECT_TIMEOUT_SECONDS:.0f} 秒未返回",
                capabilities=self.capabilities().to_dict(),
            )
        except OSError as exc:
            return DetectionResult(
                provider=self.provider, display_name=self.display_name,
                installed=True, status="error", executable=executable,
                detail=f"无法执行：{exc}",
                capabilities=self.capabilities().to_dict(),
            )

        if completed.returncode != 0:
            # Some CLIs print their version to stderr and still exit non-zero.
            detail = (completed.stderr or completed.stdout or "").strip()[:200]
            return DetectionResult(
                provider=self.provider, display_name=self.display_name,
                installed=True, status="error", executable=executable,
                detail=f"版本探测退出码 {completed.returncode}：{detail}",
                capabilities=self.capabilities().to_dict(),
            )

        version = self.parse_version(completed.stdout or completed.stderr or "")
        return DetectionResult(
            provider=self.provider, display_name=self.display_name,
            installed=True, status="available", version=version,
            executable=executable,
            capabilities=self.capabilities().to_dict(),
        )


__all__ = [
    "DETECT_TIMEOUT_SECONDS",
    "AgentCapabilities",
    "DetectionResult",
    "DetectionStatus",
    "LocalAgentAdapter",
]
