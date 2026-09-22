"""A stand-in for a launched `caffeinate`, shared by the CLI and display-wake tests.

Real `caffeinate` processes would keep a test machine's display awake for as long
as the suite runs, so nothing here spawns one.
"""


class FakeCaffeinate:
    """Records the flags it was asked for and whether it was torn down."""

    def __init__(self, arguments: list[str]):
        self.arguments = list(arguments)
        self.terminated = False
        self.returncode = None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return 0
