import logging
import time
from typing import Optional, Tuple

from openhop_core.protocol.packet_utils import calculate_lora_airtime_ms

logger = logging.getLogger("AirtimeManager")


class AirtimeManager:
    def __init__(
        self,
        config: dict,
        radio_config: Optional[dict] = None,
        max_airtime_per_minute: Optional[float] = None,
    ):
        """Meter one channel's duty cycle.

        ``radio_config`` and ``max_airtime_per_minute`` override the top-level
        ``radio`` and ``duty_cycle`` sections, for a Fabric node that meters each
        radio against its own modulation and its own band's limit. Omitted, both
        come from the config as they always have.
        """
        self.config = config
        self.radio_config = radio_config if radio_config is not None else config.get("radio", {})
        self.max_airtime_per_minute = (
            max_airtime_per_minute
            if max_airtime_per_minute is not None
            else config.get("duty_cycle", {}).get("max_airtime_per_minute", 3600)
        )

        # Store radio settings for airtime calculations
        self.refresh_radio_params(self.radio_config)

        # Track airtime in rolling window
        self.tx_history = []  # [(timestamp, airtime_ms), ...]
        self.window_size = 60  # seconds
        self.total_airtime_ms = 0
        self.total_rx_airtime_ms = 0

    def refresh_radio_params(self, radio_config: Optional[dict] = None) -> None:
        """Reload cached modulation params used by airtime estimation.

        Call after a successful live radio reconfiguration. Does not reset
        TX/RX history or duty-cycle totals.
        """
        if radio_config is None:
            radio_config = self.config.get("radio", {}) or {}
        self.radio_config = radio_config
        self.spreading_factor = self.radio_config.get("spreading_factor", 7)
        self.bandwidth = self.radio_config.get("bandwidth", 125000)
        self.coding_rate = self.radio_config.get("coding_rate", 5)
        self.preamble_length = self.radio_config.get("preamble_length", 8)

    def calculate_airtime(
        self,
        payload_len: int,
        spreading_factor: int = None,
        bandwidth_hz: int = None,
        coding_rate: int = None,
        preamble_len: int = None,
        crc_enabled: bool = True,
        explicit_header: bool = True,
    ) -> float:
        """
        Calculate LoRa packet airtime via the shared core estimator.

        Delegates to ``calculate_lora_airtime_ms``, which matches RadioLib's
        ``getTimeOnAir`` (the firmware reference), including its symbol-time
        low-data-rate-optimization auto rule. Coding rate accepts either the
        denominator form (5..8) or the legacy index form (1..4).

        Args:
            payload_len: Payload length in bytes
            spreading_factor: SF7-SF12 (uses config value if None)
            bandwidth_hz: Bandwidth in Hz (uses config value if None)
            coding_rate: CR denominator, 5=4/5, 6=4/6, 7=4/7, 8=4/8 (uses config value if None)
            preamble_len: Preamble symbols (uses config value if None)
            crc_enabled: Whether CRC is enabled (default: True)
            explicit_header: Whether explicit header mode is used (default: True)

        Returns:
            Airtime in milliseconds
        """
        return calculate_lora_airtime_ms(
            payload_len,
            spreading_factor or self.spreading_factor,
            bandwidth_hz or self.bandwidth,
            coding_rate or self.coding_rate,
            preamble_len or self.preamble_length,
            crc_enabled=crc_enabled,
            explicit_header=explicit_header,
        )

    def can_transmit(self, airtime_ms: float) -> Tuple[bool, float]:
        enforcement_enabled = self.config.get("duty_cycle", {}).get("enforcement_enabled", True)
        if not enforcement_enabled:
            # Duty cycle enforcement disabled - always allow
            return True, 0.0

        now = time.time()

        # Remove old entries outside window
        self.tx_history = [(ts, at) for ts, at in self.tx_history if now - ts < self.window_size]

        # Calculate current airtime in window
        current_airtime = sum(at for _, at in self.tx_history)

        if current_airtime + airtime_ms <= self.max_airtime_per_minute:
            return True, 0.0

        # Calculate wait time until oldest entry expires
        if self.tx_history:
            oldest_ts, oldest_at = self.tx_history[0]
            wait_time = (oldest_ts + self.window_size) - now
            return False, max(0, wait_time)

        return False, 1.0

    def record_tx(self, airtime_ms: float):
        self.tx_history.append((time.time(), airtime_ms))
        self.total_airtime_ms += airtime_ms
        logger.debug(f"TX recorded: {airtime_ms: .1f}ms (total: {self.total_airtime_ms: .0f}ms)")

    def record_rx(self, airtime_ms: float):
        """Record received packet airtime (for total RX airtime stats)."""
        self.total_rx_airtime_ms += airtime_ms

    def get_stats(self) -> dict:
        now = time.time()
        self.tx_history = [(ts, at) for ts, at in self.tx_history if now - ts < self.window_size]

        current_airtime = sum(at for _, at in self.tx_history)
        utilization = (current_airtime / self.max_airtime_per_minute) * 100

        return {
            "current_airtime_ms": current_airtime,
            "max_airtime_ms": self.max_airtime_per_minute,
            "utilization_percent": utilization,
            "total_airtime_ms": self.total_airtime_ms,
            "total_rx_airtime_ms": self.total_rx_airtime_ms,
        }


class AirtimeBudgets:
    """The duty-cycle budgets a node meters against: one per channel.

    Duty cycle is a limit on a channel, not on a node. A single manager charging
    every radio at one modulation is wrong in both directions on a dual-frequency
    bridge: the same packet occupies a 62.5 kHz channel about eight times longer
    than a 500 kHz one, so whichever bandwidth the top-level ``radio`` block
    names, the other radio is mis-charged by that factor. Sharing one budget
    compounds it -- a busy local radio spends the backhaul's allowance, on a band
    it never transmits on.

    So each radio is metered on its own, with its own modulation and its own
    band's limit. Two radios on the same channel share one manager, because they
    do contend for the same spectrum: two transmitters on 869.618 MHz are one
    channel's worth of traffic however the node labels them.

    A single-radio node gets exactly one manager built from the top-level
    sections, which is what it had before this existed.
    """

    def __init__(self, config: dict):
        self.config = config
        # radio_id -> manager, in configured order.
        self._managers: dict = {}
        self._order: list = []
        # Channel key -> manager, and radio_id -> channel key. Both are how a
        # rebuild works out what carries over to what.
        self._by_channel: dict = {}
        self._channel_of: dict = {}
        self._profile_backed = False
        self._default_manager: Optional[AirtimeManager] = None
        self._build()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        from .config import build_metering_profiles

        try:
            profiles = build_metering_profiles(self.config)
        except Exception as exc:  # pragma: no cover - metering must not fail to start
            logger.warning("Could not read radio profiles for duty cycle: %s", exc)
            profiles = []

        self._managers = {}
        self._order = []
        self._by_channel = {}
        self._channel_of = {}
        self._profile_backed = bool(profiles)

        if not profiles:
            # Nothing to read: meter exactly as before, keyed on None so a packet
            # with no radio id still finds it.
            self._order = [None]
            self._channel_of = {None: None}
            manager = AirtimeManager(self.config)
            self._managers = {None: manager}
            self._by_channel = {None: manager}
            self._default_manager = manager
            return

        shared = bool(self.config.get("duty_cycle", {}).get("shared_budget", False))
        # One budget for every radio when asked for, otherwise one per channel:
        # same frequency and bandwidth is the same channel.
        for profile in profiles:
            radio_id = str(profile["radio_id"])
            self._order.append(radio_id)
            self._channel_of[radio_id] = (
                "shared" if shared else (profile.get("frequency_hz"), profile.get("bandwidth_hz"))
            )

        for profile in profiles:
            radio_id = str(profile["radio_id"])
            key = self._channel_of[radio_id]
            manager = self._by_channel.get(key)
            if manager is None:
                manager = AirtimeManager(
                    self.config,
                    radio_config=self._air_settings(profile),
                    max_airtime_per_minute=self._channel_budget(
                        [rid for rid, rid_key in self._channel_of.items() if rid_key == key]
                    ),
                )
                self._by_channel[key] = manager
            self._managers[radio_id] = manager

        self._default_manager = self._managers[self._default_radio_id()]

    def _air_settings(self, profile: dict) -> dict:
        """A profile in the shape AirtimeManager reads air settings from.

        Laid over the top-level ``radio`` block rather than replacing it, so a
        field this radio could not report inherits the value the node would have
        metered with anyway instead of an unrelated built-in default.
        """
        settings = dict(self.config.get("radio", {}) or {})
        for key, value in (
            ("spreading_factor", profile.get("spreading_factor")),
            ("bandwidth", profile.get("bandwidth_hz")),
            ("coding_rate", profile.get("coding_rate")),
            ("preamble_length", profile.get("preamble_length")),
        ):
            if value is not None:
                settings[key] = value
        return settings

    def _channel_budget(self, radio_ids: list) -> Optional[float]:
        """The limit for a channel several radios may sit on: the strictest one.

        Taking the first radio's would let a second radio configured for a 1%
        band transmit against a 10% allowance, which is the wrong direction to
        be wrong in about a legal limit.
        """
        budgets = [self._budget_for(radio_id) for radio_id in radio_ids]
        stated = [budget for budget in budgets if budget is not None]
        if not stated:
            return None
        node_wide = self.config.get("duty_cycle", {}).get("max_airtime_per_minute", 3600)
        # A radio that states nothing is held to the node-wide limit, so it
        # counts towards the minimum rather than being ignored.
        if len(stated) != len(budgets):
            stated.append(node_wide)
        return min(stated)

    def _budget_for(self, radio_id: str) -> Optional[float]:
        """Per-radio ``duty_cycle.max_airtime_per_minute``, else the node's.

        Bands differ: 868.0-868.6 MHz allows 1% where 869.4-869.65 allows 10%,
        so a bridge spanning two sub-bands has two different legal limits and
        one number cannot describe both.
        """
        radios = self.config.get("radios")
        if not isinstance(radios, list):
            return None
        for entry in radios:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or entry.get("radio_id") or "") != radio_id:
                continue
            duty_cycle = entry.get("duty_cycle")
            if isinstance(duty_cycle, dict) and "max_airtime_per_minute" in duty_cycle:
                return duty_cycle["max_airtime_per_minute"]
        return None

    # ------------------------------------------------------------------
    # Rebuilding on a live config change
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the budgets from the current config, keeping what was spent.

        A live radio change moves a channel, and the new modulation has to meter
        the next send. What is already on the air is carried over: a retune is
        not a fresh minute, and forgetting it would let a node transmit its whole
        budget twice in one window.

        Carried between *channels* rather than between radios, because a channel
        is what a duty cycle applies to, and because it makes a rebuild
        idempotent: refreshing an unchanged config hands every channel back its
        own history exactly, however many times it is called. Two channels
        merging into one sum, because that channel really did carry both. One
        channel splitting hands each side the full history, since neither can
        prove it was the quiet one; a second refresh then finds each side's own
        key and stops there rather than doubling again.
        """
        previous_channel_of = dict(self._channel_of)
        previous_default = self._default_manager
        snapshots = {
            key: (
                list(manager.tx_history),
                manager.total_airtime_ms,
                manager.total_rx_airtime_ms,
            )
            for key, manager in self._by_channel.items()
        }

        self._build()

        for key, manager in self._by_channel.items():
            sources = {
                previous_channel_of[radio_id]
                for radio_id, radio_key in self._channel_of.items()
                if radio_key == key and radio_id in previous_channel_of
            }
            if not sources:
                # A channel nothing was metering: a radio added, or one retuned
                # onto a band the node was not using. An empty window on a node
                # that has been transmitting would let the next minute's budget
                # be spent twice, so it starts from the busiest channel the node
                # was already keeping. The busiest single one, not the sum of
                # them: summing is what turns repeated rebuilds into 100 -> 400
                # -> 1600 ms and wedges transmission for the rest of the window.
                busiest = max(
                    snapshots.values(),
                    key=lambda carried: sum(at for _, at in carried[0]),
                    default=None,
                )
                if busiest is None:
                    continue
                carried_all = [busiest]
            else:
                carried_all = [snapshots[source] for source in sources]

            history: list = []
            total_tx = 0.0
            total_rx = 0.0
            for carried in carried_all:
                history.extend(carried[0])
                total_tx += carried[1]
                total_rx += carried[2]
            manager.tx_history = sorted(history, key=lambda entry: entry[0])
            manager.total_airtime_ms = total_tx
            manager.total_rx_airtime_ms = total_rx

        self._keep_default_identity(previous_default)

    def _keep_default_identity(self, was_default: Optional[AirtimeManager]) -> None:
        """Let the object that was the default manager go on being it.

        Callers hold onto it: main.py hands it to NeighborScopeHelper at boot,
        which keeps the reference for the life of the process. Replacing it with
        a new object would leave that helper reading a manager nothing charges
        any more, so it would see an empty window and stop throttling itself.
        Adopt the rebuilt settings into the original object instead.

        Takes the previous default as an argument rather than working it out
        again: ``config`` is mutated in place by the daemon, so by the time this
        runs ``fabric.default_radio`` may already name a different radio, and
        recomputing would preserve the wrong object -- orphaning the one the
        helper is holding, which is the failure this exists to prevent.
        """
        if was_default is None or self._default_manager is None:
            return
        rebuilt = self._default_manager
        if rebuilt is was_default:
            return

        was_default.radio_config = rebuilt.radio_config
        was_default.refresh_radio_params(rebuilt.radio_config)
        was_default.max_airtime_per_minute = rebuilt.max_airtime_per_minute
        was_default.tx_history = rebuilt.tx_history
        was_default.total_airtime_ms = rebuilt.total_airtime_ms
        was_default.total_rx_airtime_ms = rebuilt.total_rx_airtime_ms
        for radio_id, manager in self._managers.items():
            if manager is rebuilt:
                self._managers[radio_id] = was_default
        for key, manager in self._by_channel.items():
            if manager is rebuilt:
                self._by_channel[key] = was_default
        self._default_manager = was_default

    def _default_radio_id(self) -> Optional[str]:
        """The radio Fabric transmits on by default.

        Mirrors build_radio_stack's rule -- ``fabric.default_radio`` when set,
        otherwise the first configured radio. Reading ``radios[0]`` instead
        silently meters a node whose default_radio is its second entry against
        the wrong channel entirely.
        """
        fabric = self.config.get("fabric")
        fabric = fabric if isinstance(fabric, dict) else {}
        configured = fabric.get("default_radio") or fabric.get("default_radio_id")
        if configured and str(configured) in self._managers:
            return str(configured)
        return self._order[0] if self._order else None

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def default(self) -> AirtimeManager:
        """The manager everything that reports one channel's figures reads."""
        return self._default_manager

    @property
    def multi(self) -> bool:
        return len(self._order) > 1

    @property
    def profile_backed(self) -> bool:
        """Whether these budgets were built from per-radio profiles.

        True for one configured ``radios[]`` entry as much as for five. A node
        with a single entry still has that entry's own modulation and its own
        band's limit, and overwriting them with the top-level block -- which is
        what happens if a caller tests ``multi`` and takes the legacy path --
        undoes the whole point on every radio save from the web UI.
        """
        return self._profile_backed

    def radio_ids(self) -> list:
        """Configured radio ids, in order. ``[None]`` when nothing was profiled."""
        return list(self._order)

    def for_radio(self, radio_id: Optional[str]) -> AirtimeManager:
        """The budget a send on this radio is charged to.

        An unknown id falls back to the default radio rather than going
        unmetered: an unrecognised label is a reason to be careful, not a reason
        to transmit freely.
        """
        if radio_id is None:
            return self.default
        manager = self._managers.get(str(radio_id))
        if manager is not None:
            return manager
        logger.debug("No duty-cycle budget for radio %s; metering on the default", radio_id)
        return self.default

    def per_radio_stats(self) -> list:
        """``[{radio_id, ...stats}]`` on a multi-radio node, else an empty list.

        Radios sharing a channel report the same figures, because they are the
        same budget: that is the statement, not a duplication.
        """
        if not self.multi:
            return []
        return [
            {"radio_id": radio_id, **self._managers[radio_id].get_stats()}
            for radio_id in self._order
        ]

    def node_stats(self) -> dict:
        """One set of figures for the whole node, in AirtimeManager's shape.

        For the callers that have always reported a single number: the MeshCore
        wire field ``total_air_time_secs``, companion stats, the RRD and SQLite
        history, and ``/stats``. Reading the default radio's manager instead
        would have each of them describe one channel of a node that transmits on
        two, which is a quiet regression in figures people have been watching
        for months.

        Totals and the current window are summed across the distinct channels,
        because the node really did spend all of it. Utilisation is the highest
        of them, not the ratio of the sums: a node whose narrow channel is at its
        legal limit is at a limit, and averaging that against an idle wide
        channel reports 14% for a radio that must stop transmitting.

        On a single-radio node every figure is that one manager's, unchanged.
        """
        managers = list(self._by_channel.values())
        if len(managers) == 1:
            return managers[0].get_stats()

        stats = [manager.get_stats() for manager in managers]
        return {
            "current_airtime_ms": sum(s["current_airtime_ms"] for s in stats),
            "max_airtime_ms": sum(s["max_airtime_ms"] for s in stats),
            "utilization_percent": max(s["utilization_percent"] for s in stats),
            "total_airtime_ms": sum(s["total_airtime_ms"] for s in stats),
            "total_rx_airtime_ms": sum(s["total_rx_airtime_ms"] for s in stats),
        }
