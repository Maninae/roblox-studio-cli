"""A stand-in for a launched `caffeinate`, shared by the CLI and display-wake tests.

Real `caffeinate` processes would keep a test machine's display awake for as long
as the suite runs, so nothing here spawns one.
"""


class FakeCaffeinate:
    """Records the flags it was asked for and whether it was torn down.

    `returncode` doubles as what `poll()` answers, so a test can hand back a
    caffeinate that died the moment it launched.
    """

    def __init__(self, arguments: list[str], returncode: int | None = None):
        self.arguments = list(arguments)
        self.terminated = False
        self.returncode = returncode

    def poll(self) -> int | None:
        """The exit status if it has already exited, None while it is running."""
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return 0
