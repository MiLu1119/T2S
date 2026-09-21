"""LangGraph checkpoint backends with state sanitization."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
from typing import Any, Iterator

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.base import get_checkpoint_metadata
from opentelemetry import trace


tracer = trace.get_tracer("verisql.checkpoint")


_TRANSIENT_CHANNELS = {"execution_rows", "final_answer", "cancel_event"}


class SanitizedCheckpointer(BaseCheckpointSaver):
    """Remove result payloads before delegating persistence to a saver."""

    def __init__(self, inner: BaseCheckpointSaver):
        super().__init__(serde=inner.serde)
        self.inner = inner

    @staticmethod
    def _checkpoint(checkpoint):
        sanitized = dict(checkpoint)
        values = dict(sanitized.get("channel_values", {}))
        sanitized["channel_values"] = values
        values.pop("execution_rows", None)
        values.pop("final_answer", None)
        values.pop("cancel_event", None)
        return sanitized

    @staticmethod
    def _writes(writes):
        return [(channel, value) for channel, value in writes if channel not in _TRANSIENT_CHANNELS]

    @tracer.start_as_current_span("verisql.checkpoint.read")
    def get_tuple(self, config):
        result = self.inner.get_tuple(config)
        span = trace.get_current_span()
        span.set_attribute("verisql.checkpoint.backend", type(self.inner).__name__)
        span.set_attribute("verisql.checkpoint.hit", result is not None)
        return result

    def list(self, config, *, filter=None, before=None, limit=None):
        return self.inner.list(config, filter=filter, before=before, limit=limit)

    @tracer.start_as_current_span("verisql.checkpoint.write")
    def put(self, config, checkpoint, metadata, new_versions):
        trace.get_current_span().set_attribute(
            "verisql.checkpoint.backend", type(self.inner).__name__
        )
        return self.inner.put(config, self._checkpoint(checkpoint), metadata, new_versions)

    @tracer.start_as_current_span("verisql.checkpoint.pending_writes")
    def put_writes(self, config, writes, task_id, task_path=""):
        sanitized = self._writes(writes)
        span = trace.get_current_span()
        span.set_attribute("verisql.checkpoint.backend", type(self.inner).__name__)
        span.set_attribute("verisql.checkpoint.write_count", len(sanitized))
        return self.inner.put_writes(config, sanitized, task_id, task_path)

    @tracer.start_as_current_span("verisql.checkpoint.delete_thread")
    def delete_thread(self, thread_id):
        trace.get_current_span().set_attribute(
            "verisql.checkpoint.backend", type(self.inner).__name__
        )
        return self.inner.delete_thread(thread_id)

    def get_next_version(self, current, channel):
        return self.inner.get_next_version(current, channel)

    async def aget_tuple(self, config):
        return await self.inner.aget_tuple(config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        async for item in self.inner.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self.inner.aput(config, self._checkpoint(checkpoint), metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self.inner.aput_writes(config, self._writes(writes), task_id, task_path)

    async def adelete_thread(self, thread_id):
        return await self.inner.adelete_thread(thread_id)


class PlainRedisSaver(BaseCheckpointSaver):
    """Latest-checkpoint saver for ordinary Redis without Search/JSON modules."""

    def __init__(self, redis_url: str, ttl_minutes: int = 0):
        from redis import Redis

        super().__init__()
        self.client = Redis.from_url(redis_url, decode_responses=False)
        self.ttl_seconds = max(0, ttl_minutes) * 60
        self.client.ping()

    @staticmethod
    def _thread_hash(thread_id: str) -> str:
        return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()

    def _key(self, thread_id: str, checkpoint_ns: str) -> str:
        namespace = hashlib.sha256(checkpoint_ns.encode("utf-8")).hexdigest()[:16]
        return f"verisql:checkpoint:{self._thread_hash(thread_id)}:{namespace}"

    def _writes_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._key(thread_id, checkpoint_ns)}:writes:{checkpoint_id}"

    def _expire(self, *keys: str) -> None:
        if self.ttl_seconds:
            for key in keys:
                self.client.expire(key, self.ttl_seconds)

    def get_tuple(self, config):
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        key = self._key(thread_id, checkpoint_ns)
        data = self.client.hgetall(key)
        if not data:
            return None
        checkpoint = self.serde.loads_typed(
            (data[b"checkpoint_type"].decode(), data[b"checkpoint_blob"])
        )
        requested_id = config["configurable"].get("checkpoint_id")
        if requested_id and requested_id != checkpoint["id"]:
            return None
        metadata = self.serde.loads_typed(
            (data[b"metadata_type"].decode(), data[b"metadata_blob"])
        )
        writes_key = self._writes_key(thread_id, checkpoint_ns, checkpoint["id"])
        pending_writes = []
        for raw in self.client.hvals(writes_key):
            value_type, blob = raw.split(b"\0", 1)
            pending_writes.append(self.serde.loads_typed((value_type.decode(), blob)))
        self._expire(key, writes_key)
        parent_id = data.get(b"parent_id", b"").decode()
        stored_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }
        return CheckpointTuple(
            config=stored_config,
            checkpoint=checkpoint,
            metadata=metadata,
            pending_writes=pending_writes,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
        )

    def list(self, config, *, filter=None, before=None, limit=None):
        if config is None or limit == 0:
            return iter(())
        item = self.get_tuple(config)
        if item is None:
            return iter(())
        if filter and not all(item.metadata.get(k) == v for k, v in filter.items()):
            return iter(())
        before_id = (before or {}).get("configurable", {}).get("checkpoint_id")
        if before_id and item.checkpoint["id"] >= before_id:
            return iter(())
        return iter((item,))

    def put(self, config, checkpoint, metadata, new_versions):
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        key = self._key(thread_id, checkpoint_ns)
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_blob = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        parent_id = config["configurable"].get("checkpoint_id", "")
        self.client.hset(
            key,
            mapping={
                "checkpoint_type": checkpoint_type,
                "checkpoint_blob": checkpoint_blob,
                "metadata_type": metadata_type,
                "metadata_blob": metadata_blob,
                "parent_id": parent_id,
            },
        )
        self._expire(key)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(self, config, writes, task_id, task_path=""):
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        key = self._writes_key(thread_id, checkpoint_ns, checkpoint_id)
        mapping = {}
        for index, (channel, value) in enumerate(writes):
            value_type, blob = self.serde.dumps_typed((task_id, channel, value))
            mapping[f"{task_id}:{index}"] = value_type.encode() + b"\0" + blob
        if mapping:
            self.client.hset(key, mapping=mapping)
            self._expire(key)

    def delete_thread(self, thread_id):
        pattern = f"verisql:checkpoint:{self._thread_hash(thread_id)}:*"
        keys = list(self.client.scan_iter(match=pattern, count=100))
        if keys:
            self.client.delete(*keys)

    def close(self) -> None:
        self.client.close()


@contextmanager
def open_checkpointer(settings, root: Path) -> Iterator[BaseCheckpointSaver | None]:
    """Open the configured official LangGraph saver for the app lifespan."""

    backend = settings.checkpoint_backend
    if backend == "none":
        yield None
        return

    if backend == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = Path(settings.checkpoint_sqlite_path)
        if not path.is_absolute():
            path = root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(path)) as saver:
            yield SanitizedCheckpointer(saver)
        return

    if backend in {"redis", "redis-stack"}:
        if not settings.redis_url:
            raise ValueError("REDIS_URL is required when CHECKPOINT_BACKEND=redis")
        if backend == "redis":
            saver = PlainRedisSaver(
                settings.redis_url,
                ttl_minutes=settings.checkpoint_ttl_minutes,
            )
            try:
                yield SanitizedCheckpointer(saver)
            finally:
                saver.close()
            return
        from langgraph.checkpoint.redis.shallow import ShallowRedisSaver

        ttl: dict[str, Any] | None = None
        if settings.checkpoint_ttl_minutes > 0:
            ttl = {
                "default_ttl": settings.checkpoint_ttl_minutes,
                "refresh_on_read": True,
            }
        with ShallowRedisSaver.from_conn_string(settings.redis_url, ttl=ttl) as saver:
            saver.setup()
            yield SanitizedCheckpointer(saver)
        return

    raise ValueError(f"Unsupported checkpoint backend: {backend}")
