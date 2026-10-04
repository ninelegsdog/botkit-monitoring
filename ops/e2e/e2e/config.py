from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Settings:
    """Runtime configuration for the E2E runner.

    The account is a user account, not a bot: the runner authenticates once with
    api_id/api_hash and then talks to each bot as a human would. That is why
    `phone` is optional - a session that already carries an authorized key does
    not need to be re-authorized, and requiring a phone number would have made
    every later run depend on a credential the E2E host must not keep.
    """

    api_id: int
    api_hash: str
    phone: str = ""
    bots_file: Path = Path("bots.yml")
    status_dir: Path = Path("/var/lib/botkit-e2e/status")
    session_dir: Path = Path("/var/lib/botkit-e2e/session")
    session_name: str = "botkit-e2e"
    scenarios_file: str = "scenarios.yml"
    alert_url: str = ""
    # Deliberate opt-in for runs nobody intends to act on. Off by default,
    # because a green result that cannot page anyone is not a test result.
    allow_no_alert: bool = False
    # Page the owner through the E2E session's own Saved Messages. Alertmanager
    # only listens on the prod loopback and prod forbids TCP forwarding, so
    # there is no route from this host to it; the session needs no bot token.
    alert_telegram: bool = True
    # Watchdog: report the run's own outcome too. Per-bot failures page on their
    # own, so without this a runner that died mid-run and a healthy fleet would
    # look identical from the outside - silence would mean nothing.
    watchdog: bool = True
    # How long a completed run may stay silent before the external watchdog
    # calls the runner dead. Must exceed the E2E timer's interval (6h) plus its
    # jitter (up to 30m) plus the run itself, or the watchdog reports outages
    # that are nothing but the schedule. Detection latency after that is the
    # watchdog timer's cadence, not this number.
    watchdog_max_gap_min: int = 480
    timeout: int = 30
    device_model: str = "botkit-e2e"
    system_version: str = "Linux"
    app_version: str = "botkit-e2e/1.0"
    # "socks5:host:port" or "socks5h:host:port"; empty means connect directly.
    # The E2E host egress is blackholed by Telegram (see the ADR note on the
    # proxy), so the runner reaches the DC through a loopback SOCKS proxy.
    proxy: str = ""

    def __post_init__(self) -> None:
        # Paths are coerced here rather than trusted at every use site: a session
        # file built by joining a str and a Path raises deep inside telethon's
        # constructor, where the message says nothing about configuration.
        self.bots_file = Path(self.bots_file)
        self.status_dir = Path(self.status_dir)
        self.session_dir = Path(self.session_dir)

    @property
    def session_path(self) -> Path:
        """Absolute path of the SQLite session, kept outside $HOME by W21."""
        return self.session_dir / f"{self.session_name}.session"


@dataclass
class Step:
    send: str
    expect: str


@dataclass
class Scenario:
    steps: list[Step] = field(default_factory=list)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer, got {raw!r}"
        raise ValueError(msg) from exc


def load_settings() -> Settings:
    return Settings(
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        phone=os.environ.get("TG_PHONE", ""),
        bots_file=Path(os.environ.get("E2E_BOTS_FILE", "bots.yml")),
        status_dir=Path(os.environ.get("E2E_STATUS_DIR", "/var/lib/botkit-e2e/status")),
        session_dir=Path(os.environ.get("E2E_SESSION_DIR", "/var/lib/botkit-e2e/session")),
        session_name=os.environ.get("E2E_SESSION_NAME", "botkit-e2e"),
        scenarios_file=os.environ.get("E2E_SCENARIOS", "scenarios.yml"),
        alert_url=os.environ.get("E2E_ALERT_URL", ""),
        allow_no_alert=os.environ.get("E2E_ALLOW_NO_ALERT", "").strip().lower()
        in ("1", "true", "yes", "on"),
        alert_telegram=os.environ.get("E2E_ALERT_TELEGRAM", "1").strip().lower()
        in ("1", "true", "yes", "on"),
        watchdog=os.environ.get("E2E_WATCHDOG", "1").strip().lower()
        in ("1", "true", "yes", "on"),
        watchdog_max_gap_min=int(os.environ.get("E2E_WATCHDOG_MAX_GAP_MIN", "480")),
        proxy=os.environ.get("E2E_PROXY", ""),
        timeout=_int_env("E2E_TIMEOUT", 30),
        device_model=os.environ.get("E2E_DEVICE_MODEL", "botkit-e2e"),
        system_version=os.environ.get("E2E_SYSTEM_VERSION", "Linux"),
        app_version=os.environ.get("E2E_APP_VERSION", "botkit-e2e/1.0"),
    )


def load_scenarios(path) -> dict[str, Scenario]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {
        b: Scenario(steps=[Step(s["send"], s["expect"]) for s in c.get("steps", [])])
        for b, c in data.items()
    }


def load_bots(path) -> dict[str, str]:
    """Map bot directory name -> Telegram username.

    The runner resolves usernames once, on the machine that can read the bot
    tokens, and commits the result. Nothing in the E2E path needs a token
    afterwards, so no bot token is ever read by the runner itself.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    missing = sorted(b for b, username in data.items() if not username)
    if missing:
        msg = f"bots.yml has empty usernames for: {', '.join(missing)}"
        raise ValueError(msg)
    return {str(b): str(u).lstrip("@") for b, u in data.items()}
