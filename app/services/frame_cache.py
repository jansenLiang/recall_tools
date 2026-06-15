from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


H264_NAL_START_CODE_4 = b"\x00\x00\x00\x01"
H264_NAL_START_CODE_3 = b"\x00\x00\x01"
H264_NAL_TYPE_MASK = 0x1F
H264_NAL_IDR = 5
H264_NAL_SPS = 7
H264_NAL_PPS = 8
H264_NAL_SEI = 6


def _nal_unit_type(byte: int) -> int:
    return byte & H264_NAL_TYPE_MASK


def _find_start_codes(annexb: bytes) -> list[tuple[int, int]]:
    """Return list of (position, start_code_length) for each NAL start code."""
    results: list[tuple[int, int]] = []
    i = 0
    n = len(annexb)
    while i < n - 3:
        if annexb[i : i + 4] == H264_NAL_START_CODE_4:
            results.append((i, 4))
            i += 4
        elif annexb[i : i + 3] == H264_NAL_START_CODE_3:
            results.append((i, 3))
            i += 3
        else:
            i += 1
    return results


@dataclass
class CachedFrame:
    participant_id: str
    frame_type: str
    received_at: float
    width: int | None = None
    height: int | None = None
    annexb_bytes: bytes = b""

    def size_bytes(self) -> int:
        return len(self.annexb_bytes)


@dataclass
class _BucketAccumulator:
    sps: bytes | None = None
    pps: bytes | None = None
    idr_annexb: bytearray = field(default_factory=bytearray)
    cached: CachedFrame | None = None


class H264FrameCache:
    """In-memory cache of the most recent IDR H.264 frame per (participant_id, type).

    Designed to receive base64 NAL units from Recall.ai's `video_separate_h264.data`
    WebSocket event, accumulate NALs into an AnnexB byte stream, and retain only
    the most recent IDR frame per bucket. SPS/PPS are kept on the bucket (not in
    the cached frame) so each commit produces a self-contained annexb blob.
    """

    def __init__(
        self,
        *,
        max_participants: int = 32,
        max_age_seconds: float = 300.0,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.max_participants = max(1, max_participants)
        self.max_age_seconds = max(1.0, float(max_age_seconds))
        self.max_bytes = max(1024 * 1024, max_bytes)
        self._buckets: "OrderedDict[tuple[str, str], _BucketAccumulator]" = OrderedDict()
        self._lock = asyncio.Lock()
        self._total_bytes = 0

    @staticmethod
    def _decode_nal_payload(buffer_b64: str) -> bytes:
        if not buffer_b64:
            return b""
        try:
            return base64.b64decode(buffer_b64, validate=False)
        except (ValueError, TypeError):
            logger.warning("frame cache: invalid base64 in h264 buffer; dropping chunk")
            return b""

    @staticmethod
    def _split_nals(annexb: bytes) -> list[tuple[int, bytes]]:
        if not annexb:
            return []
        chunks: list[tuple[int, bytes]] = []
        positions = _find_start_codes(annexb)
        for i, (pos, sclen) in enumerate(positions):
            end = positions[i + 1][0] if i + 1 < len(positions) else len(annexb)
            nal = annexb[pos:end]
            if nal:
                chunks.append((sclen, nal))
        return chunks

    async def ingest(
        self,
        *,
        participant_id: str,
        frame_type: str,
        buffer_b64: str,
    ) -> None:
        if not participant_id or frame_type not in {"webcam", "screenshare"}:
            return
        chunk = self._decode_nal_payload(buffer_b64)
        if not chunk:
            return
        async with self._lock:
            bucket = self._buckets.get((participant_id, frame_type))
            if bucket is None:
                if len(self._buckets) >= self.max_participants * 2:
                    self._evict_oldest()
                bucket = _BucketAccumulator()
                self._buckets[(participant_id, frame_type)] = bucket
            self._buckets.move_to_end((participant_id, frame_type))

            nals = self._split_nals(chunk)
            for sclen, nal in nals:
                if len(nal) <= sclen:
                    continue
                nal_type = _nal_unit_type(nal[sclen])
                if nal_type == H264_NAL_SPS:
                    bucket.sps = nal
                    continue
                if nal_type == H264_NAL_PPS:
                    bucket.pps = nal
                    continue
                if nal_type == H264_NAL_SEI:
                    continue
                if nal_type == H264_NAL_IDR:
                    bucket.idr_annexb = bytearray()
                    bucket.idr_annexb.extend(nal)
                    self._commit_idr(bucket, participant_id, frame_type)
                    continue

    def _commit_idr(
        self,
        bucket: _BucketAccumulator,
        participant_id: str,
        frame_type: str,
    ) -> None:
        annexb = bytearray()
        if bucket.sps:
            annexb.extend(bucket.sps)
        if bucket.pps:
            annexb.extend(bucket.pps)
        annexb.extend(bucket.idr_annexb)
        if bucket.cached is not None:
            self._total_bytes -= bucket.cached.size_bytes()
        now = time.time()
        bucket.cached = CachedFrame(
            participant_id=participant_id,
            frame_type=frame_type,
            received_at=now,
            annexb_bytes=bytes(annexb),
        )
        self._total_bytes += bucket.cached.size_bytes()
        self._enforce_budget()

    def _evict_oldest(self) -> None:
        while self._buckets and (
            len(self._buckets) >= self.max_participants * 2
            or self._total_bytes > self.max_bytes
        ):
            key, bucket = self._buckets.popitem(last=False)
            if bucket.cached is not None:
                self._total_bytes -= bucket.cached.size_bytes()

    def _enforce_budget(self) -> None:
        if self._total_bytes <= self.max_bytes and len(self._buckets) <= self.max_participants * 2:
            return
        self._evict_oldest()

    def _prune_locked(self, now: float) -> None:
        expired_keys: list[tuple[str, str]] = []
        for key, bucket in self._buckets.items():
            if bucket.cached is None:
                continue
            if now - bucket.cached.received_at > self.max_age_seconds:
                expired_keys.append(key)
        for key in expired_keys:
            bucket = self._buckets.pop(key, None)
            if bucket and bucket.cached is not None:
                self._total_bytes -= bucket.cached.size_bytes()

    async def get_frame(
        self,
        *,
        participant_id: str,
        frame_type: str = "screenshare",
    ) -> CachedFrame | None:
        async with self._lock:
            self._prune_locked(time.time())
            bucket = self._buckets.get((participant_id, frame_type))
            if bucket is None or bucket.cached is None:
                return None
            return bucket.cached

    async def latest_frame(
        self,
        *,
        frame_type: str = "screenshare",
    ) -> CachedFrame | None:
        async with self._lock:
            self._prune_locked(time.time())
            best: tuple[float, tuple[str, str], CachedFrame] | None = None
            for key, bucket in self._buckets.items():
                pid, ftype = key
                if ftype != frame_type or bucket.cached is None:
                    continue
                if best is None or bucket.cached.received_at > best[0]:
                    best = (bucket.cached.received_at, key, bucket.cached)
            if best is None:
                return None
            return best[2]

    async def list_participants(self) -> list[dict[str, Any]]:
        async with self._lock:
            self._prune_locked(time.time())
            out: list[dict[str, Any]] = []
            for (pid, ftype), bucket in self._buckets.items():
                if bucket.cached is None:
                    continue
                out.append(
                    {
                        "participant_id": pid,
                        "type": ftype,
                        "received_at": bucket.cached.received_at,
                        "width": bucket.cached.width,
                        "height": bucket.cached.height,
                    }
                )
            out.sort(key=lambda item: item["received_at"], reverse=True)
            return out

    async def clear(self) -> None:
        async with self._lock:
            self._buckets.clear()
            self._total_bytes = 0

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "buckets": len(self._buckets),
                "total_bytes": self._total_bytes,
                "max_participants": self.max_participants,
                "max_age_seconds": self.max_age_seconds,
                "max_bytes": self.max_bytes,
            }
