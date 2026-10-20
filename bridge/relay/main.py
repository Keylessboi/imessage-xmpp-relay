"""Service entry point: wires config, transport, upstream and shutdown."""
from __future__ import annotations

import asyncio
import signal
import sys
from typing import Optional

from .component import ComponentTransport
from .config import Config, ConfigError
from .calls import CallService
from .core import Bridge
from .logsetup import get_logger, setup_logging
from .upstream import Backoff, UpstreamClient, UpstreamError
from .xmpp_side import SlixmppTransport, XmppTransport

log = get_logger("relay.main")


def make_transport(cfg: Config) -> XmppTransport:
    """Pick the XMPP transport for the configured mode.

    Client mode (the default) keeps the existing logged-in bot account;
    component mode uses a XEP-0114 external component owning a domain.
    """
    if cfg.mode == "component":
        return ComponentTransport(cfg)
    return SlixmppTransport(cfg)


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Turn SIGTERM/SIGINT into a clean shutdown request."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-unix
            signal.signal(sig, lambda *_: stop.set())


async def probe_upstream(upstream: UpstreamClient, timeout: float = 5.0) -> None:
    """Best-effort startup health check; failure is logged, never fatal."""
    try:
        health = await asyncio.wait_for(upstream.health(), timeout=timeout)
        log.info(
            "imessage-server health",
            event="upstream_health",
            ok=health.get("ok"),
            registered=health.get("registered"),
        )
        if not health.get("registered"):
            log.warning(
                "imessage-server reports registered=false: sending will fail until a "
                "handle is registered",
                event="upstream_unregistered",
            )
    except (UpstreamError, asyncio.TimeoutError, TimeoutError) as exc:
        log.warning("imessage-server health check failed", event="upstream_health_error", error=str(exc))


async def run_service(
    cfg: Config,
    *,
    transport: Optional[XmppTransport] = None,
    upstream: Optional[UpstreamClient] = None,
    stop: Optional[asyncio.Event] = None,
) -> None:
    """Run until SIGTERM/SIGINT, then shut every component down."""
    upstream = upstream or UpstreamClient(
        cfg.api_url,
        timeout=cfg.request_timeout,
        backoff=Backoff(
            initial=cfg.backoff_initial,
            factor=cfg.backoff_factor,
            max_delay=cfg.backoff_max,
            jitter=cfg.backoff_jitter,
        ),
        healthy_stream_after=cfg.healthy_stream_after,
    )
    transport = transport or make_transport(cfg)
    bridge = Bridge(cfg, transport, upstream)
    if isinstance(transport, ComponentTransport):
        transport.attach_calls(
            CallService(cfg, upstream, transport.send_message, transport.send_raw)
        )
    stop = stop or asyncio.Event()
    install_signal_handlers(stop)

    await probe_upstream(upstream)
    try:
        status = await asyncio.wait_for(upstream.status(), timeout=5.0)
        log.info(
            "imessage-server status",
            event="upstream_status",
            registered=status.get("registered"),
            handle_count=len(status.get("handles") or []),
        )
    except (UpstreamError, asyncio.TimeoutError, TimeoutError) as exc:
        log.warning("could not read /im/status", event="upstream_status_error", error=str(exc))

    stop_task = asyncio.create_task(stop.wait(), name="stop")
    sse_task = asyncio.create_task(
        upstream.run_event_stream(bridge.on_event, stop), name="sse"
    )
    xmpp_task = asyncio.create_task(transport.start(), name="xmpp")
    log.info("bridge running", event="started", config=cfg.describe())

    try:
        done, _ = await asyncio.wait(
            {stop_task, sse_task, xmpp_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in (sse_task, xmpp_task):
            if task in done and task.exception() is not None:
                log.error(
                    "worker exited unexpectedly",
                    event="worker_failed",
                    worker=task.get_name(),
                    error=repr(task.exception()),
                )
            elif task in done:
                log.info("worker finished", event="worker_finished", worker=task.get_name())
    except asyncio.CancelledError:
        raise
    finally:
        log.info("shutting down", event="shutdown", reason="signal_or_worker_exit")
        for task in (stop_task, sse_task, xmpp_task):
            task.cancel()
        await asyncio.gather(stop_task, sse_task, xmpp_task, return_exceptions=True)
        await transport.stop()
        await upstream.aclose()
        log.info("shutdown complete", event="stopped", stats=dict(bridge.stats))


def main(argv: Optional[list[str]] = None) -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print("configuration error: " + str(exc), file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)
    log.info("configuration", event="config", config=cfg.describe())
    try:
        asyncio.run(run_service(cfg))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        log.info("interrupted", event="interrupted")
    return 0
