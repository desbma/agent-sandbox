"""Time real sandbox launches to bound the sandbox-coding-agent launcher's overhead."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from test_integration import SCRIPT_PATH, SandboxTestCase

PERFORMANCE_ENV_VAR = "SANDBOX_CODING_AGENT_PERFORMANCE"
PERFORMANCE_ENABLED = os.environ.get(PERFORMANCE_ENV_VAR) is not None
SKIP_REASON = f"Set {PERFORMANCE_ENV_VAR} to time real sandbox launches"
# hyperfine from the test runner's PATH, absent from the synthetic environment's
HYPERFINE_BIN = shutil.which("hyperfine")
WARMUP_RUNS = 3
MAX_MEDIAN_LAUNCH_OVERHEAD_SECONDS = 0.2


@unittest.skipUnless(PERFORMANCE_ENABLED, SKIP_REASON)
class LaunchOverheadTests(SandboxTestCase):
    """Benchmarks of the launcher against direct agent runs."""

    def setUp(self) -> None:
        """Create the synthetic tree, with the project as a jj repository."""
        super().setUp()
        self.make_project_jj_repo()

    def assert_launch_overhead(self, agent: Path) -> None:
        """Time agent run through the launcher and directly, report both medians, and bound their difference."""
        assert HYPERFINE_BIN is not None
        result = subprocess.run(
            [
                HYPERFINE_BIN,
                "--warmup",
                str(WARMUP_RUNS),
                "--style=none",
                "--export-json=-",
                shlex.join([sys.executable, str(SCRIPT_PATH), str(agent)]),
                shlex.quote(str(agent)),
            ],
            cwd=self.fixture.project_dir,
            env=self.fixture.env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Hyperfine failed ({result.returncode}): {result.stderr}"
            )

        launch_median, direct_median = (
            run["summary"]["time_wall_clock"]["median"]
            for run in json.loads(result.stdout)["results"]
        )
        print(
            f"Median wall time of {agent.name}: {launch_median * 1000:.1f} ms through the launcher, {direct_median * 1000:.1f} ms directly",
            file=sys.stderr,
        )
        self.assertLess(
            launch_median - direct_median, MAX_MEDIAN_LAUNCH_OVERHEAD_SECONDS
        )

    def test_claude_launch_overhead(self) -> None:
        """Add a bounded wall time to a claude run."""
        self.assert_launch_overhead(
            self.make_executable(self.fixture.probe_dir / "claude")
        )

    def test_unknown_agent_launch_overhead(self) -> None:
        """Add a bounded wall time to the run of an agent the launcher has no spec for."""
        self.assert_launch_overhead(Path("/usr/bin/true"))


if __name__ == "__main__":
    unittest.main()
