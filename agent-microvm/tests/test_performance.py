"""Time warm VM sessions to bound the agent-microvm launcher's startup time."""

import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "agent-microvm"
PERFORMANCE_ENV_VAR = "AGENT_MICROVM_PERFORMANCE"
PERFORMANCE_ENABLED = os.environ.get(PERFORMANCE_ENV_VAR) is not None
SKIP_REASON = f"Set {PERFORMANCE_ENV_VAR} to time real VM sessions"
WARMUP_RUNS = 1
MAX_MEDIAN_SESSION_SECONDS = 5.0
BENCHMARK_TIMEOUT_SECONDS = 600
TERMINATION_GRACE_SECONDS = 5


@unittest.skipUnless(PERFORMANCE_ENABLED, SKIP_REASON)
class SessionTimeTests(unittest.TestCase):
    """Benchmarks of whole VM sessions on a warm artifact cache."""

    def test_warm_session_time(self) -> None:
        """Boot a VM, run a trivial command and power off within a bounded wall time."""
        result = subprocess.run(
            [
                "timeout",
                "--verbose",
                "--signal=INT",
                f"--kill-after={TERMINATION_GRACE_SECONDS}s",
                f"{BENCHMARK_TIMEOUT_SECONDS}s",
                "hyperfine",
                "--warmup",
                str(WARMUP_RUNS),
                "--style=none",
                "--export-json=-",
                shlex.join([sys.executable, str(SCRIPT_PATH), "true"]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Hyperfine failed ({result.returncode}): {result.stderr}"
            )

        (run,) = json.loads(result.stdout)["results"]
        median = run["summary"]["time_wall_clock"]["median"]
        print(
            f"Median wall time of a warm VM session: {median * 1000:.1f} ms",
            file=sys.stderr,
        )
        self.assertLess(median, MAX_MEDIAN_SESSION_SECONDS)


if __name__ == "__main__":
    unittest.main()
