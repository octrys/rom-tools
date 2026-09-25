"""Read a pcap and reassemble each TCP half-stream into a byte blob.

A "half-stream" is one direction of one TCP connection (a fixed 4-tuple). We
group segments by that tuple, order them by sequence number, drop pure
retransmits, and concatenate the payloads. The result is the raw application
byte stream the protocol framer then splits into messages.

We deliberately keep this simple: no SACK/overlap surgery, no out-of-order gap
filling beyond seq ordering. Game captures are typically clean loopback/LAN
traffic; if that assumption breaks we revisit here.
"""

from __future__ import annotations

import bisect
import logging
import socket
from dataclasses import dataclass, field
from pathlib import Path

import dpkt

logger = logging.getLogger(__name__)


@dataclass
class HalfStream:
    """One direction of one TCP connection, reassembled into ``data``.

    ``seg_offsets`` / ``seg_times`` record, for each contributing segment, the
    byte offset at which it starts in ``data`` and its capture timestamp, so a
    frame's offset can be mapped back to the time it was delivered.
    """

    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    data: bytes = b""
    segments: int = 0
    seg_offsets: list[int] = field(default_factory=list)
    seg_times: list[float] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, int, str, int]:
        return (self.src_ip, self.src_port, self.dst_ip, self.dst_port)

    @property
    def label(self) -> str:
        return f"{self.src_ip}:{self.src_port} -> {self.dst_ip}:{self.dst_port}"

    def time_at(self, offset: int) -> float | None:
        """Capture timestamp of the segment that delivered byte ``offset``."""
        if not self.seg_offsets:
            return None
        index = bisect.bisect_right(self.seg_offsets, offset) - 1
        index = max(index, 0)
        return self.seg_times[index]


@dataclass
class _Accumulator:
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    base_seq: int | None = None
    # relative offset -> (payload, capture timestamp)
    chunks: dict[int, tuple[bytes, float]] = field(default_factory=dict)
    segments: int = 0

    def add(self, seq: int, payload: bytes, timestamp: float) -> None:
        if not payload:
            return
        if self.base_seq is None:
            self.base_seq = seq
        offset = seq - self.base_seq
        # Ignore pure retransmits of already-seen offsets; keep first copy.
        self.chunks.setdefault(offset, (payload, timestamp))
        self.segments += 1

    def assemble(self) -> HalfStream:
        data = bytearray()
        seg_offsets: list[int] = []
        seg_times: list[float] = []
        gap_bytes = 0
        gap_count = 0
        for offset in sorted(self.chunks):
            payload, timestamp = self.chunks[offset]
            if offset > len(data):
                # Missing segment (dropped in capture). Pad with zeros so later
                # offsets stay aligned — frames past the gap keep their position
                # and the per-direction keystream does not shift. Frames that
                # overlap the padding decode wrong, but the stream re-syncs.
                gap_bytes += offset - len(data)
                gap_count += 1
                data.extend(b"\x00" * (offset - len(data)))
            elif offset < len(data):
                continue  # overlapping retransmit already covered
            seg_offsets.append(len(data))
            seg_times.append(timestamp)
            data.extend(payload)
        if gap_count:
            logger.warning(
                "stream %s:%d->%s:%d has %d gap(s) totalling %d missing bytes; "
                "padded with zeros (frames spanning gaps will misdecode)",
                self.src_ip,
                self.src_port,
                self.dst_ip,
                self.dst_port,
                gap_count,
                gap_bytes,
            )
        return HalfStream(
            src_ip=self.src_ip,
            src_port=self.src_port,
            dst_ip=self.dst_ip,
            dst_port=self.dst_port,
            data=bytes(data),
            segments=self.segments,
            seg_offsets=seg_offsets,
            seg_times=seg_times,
        )


def _ip_str(raw: bytes) -> str:
    family = socket.AF_INET if len(raw) == 4 else socket.AF_INET6
    return socket.inet_ntop(family, raw)


def read_half_streams(path: Path) -> list[HalfStream]:
    """Reassemble every TCP half-stream carrying payload from ``path``."""

    accumulators: dict[tuple[str, int, str, int], _Accumulator] = {}
    with path.open("rb") as handle:
        reader = dpkt.pcap.Reader(handle)
        for timestamp, buf in reader:
            eth = dpkt.ethernet.Ethernet(buf)
            ip = eth.data
            if not isinstance(ip, (dpkt.ip.IP, dpkt.ip6.IP6)):
                continue
            tcp = ip.data
            if not isinstance(tcp, dpkt.tcp.TCP):
                continue
            payload = bytes(tcp.data)
            if not payload:
                continue
            key = (_ip_str(ip.src), tcp.sport, _ip_str(ip.dst), tcp.dport)
            acc = accumulators.get(key)
            if acc is None:
                acc = _Accumulator(*key)
                accumulators[key] = acc
            acc.add(tcp.seq, payload, timestamp)

    streams = [acc.assemble() for acc in accumulators.values()]
    logger.info("reassembled %d half-streams from %s", len(streams), path.name)
    return streams
