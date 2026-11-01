"""NDJSON 进程适配器：背压、时限与异常翻译集中在宿主 seam。"""
from __future__ import annotations

import asyncio
from typing import Any, Protocol
from .errors import failure_from_rpc


class PluginTransport(Protocol):
    @property
    def closed(self) -> bool: ...
    @property
    def process(self) -> asyncio.subprocess.Process | None: ...
    async def start(self) -> None: ...
    async def call(self, method: str, params: dict[str, Any], timeout: float) -> Any: ...
    async def stream(self, method: str, params: dict[str, Any], timeout: float): ...
    async def close(self) -> None: ...


RPC_MAX_BYTES = 64 * 1024 * 1024


class ProcessTransport:
    @property
    def closed(self) -> bool:
        return self._closed or self._process is not None and self._process.returncode is not None

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self._process

    """每个业务插件独占一个 Node 进程，崩溃不进入 Python 宿主。"""

    def __init__(self, command: list[str], cwd: str):
        self._command = command
        self._cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._counter = 0
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        if self._process is not None:
            if self._process.returncode is not None:
                raise RuntimeError(f"插件进程已退出，退出码 {self._process.returncode}")
            return
        self._process = await asyncio.create_subprocess_exec(
            *self._command, cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=RPC_MAX_BYTES,
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def exit_error(self) -> RuntimeError:
        detail = ""
        if self._process and self._process.stderr:
            detail = (await self._process.stderr.read()).decode(errors="replace").strip()
        code = self._process.returncode if self._process else None
        return RuntimeError(f"插件进程退出，退出码 {code}: {detail[-2000:]}")

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        error: Exception = RuntimeError("插件进程已退出")
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = __import__("json").loads(line)
                    request_id = message.get("id")
                    if not isinstance(request_id, int):
                        continue
                    if "method" in message:
                        queue = self._events.get(request_id)
                        if queue:
                            try:
                                await asyncio.wait_for(queue.put(message), 0.25)
                            except TimeoutError:
                                self._events.pop(request_id, None)
                                self._cancel(request_id)
                                queue.get_nowait()
                                queue.put_nowait({'method':'error','params':{'message':'stream buffer exceeded'}})
                    elif request_id in self._pending and not self._pending[request_id].done():
                        self._pending[request_id].set_result(message)
                except (ValueError, TypeError):
                    continue
        except asyncio.CancelledError:
            return
        except Exception as exc:
            error = exc
        else:
            if not self._closed and self._process and self._process.returncode is not None:
                error = await self.exit_error()
        finally:
            self._closed = True
            if self._process.returncode is None:
                self._process.terminate()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            for queue in self._events.values():
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait({"method": "end", "params": {"error": str(error)}})

    async def _write(self, message: dict[str, Any]) -> None:
        if self._closed or not self._process or not self._process.stdin or self._process.returncode is not None:
            raise RuntimeError("插件进程不可用")
        async with self._lock:
            self._process.stdin.write((__import__("json").dumps(message, separators=(",", ":")) + "\n").encode())
            await self._process.stdin.drain()

    def _cancel(self, request_id: int) -> None:
        if self._process and self._process.stdin and self._process.returncode is None:
            message = {"jsonrpc": "2.0", "method": "request.cancel", "params": {"id": request_id}}
            self._process.stdin.write((__import__("json").dumps(message, separators=(",", ":")) + "\n").encode())

    def _new_request(self, streaming: bool = False) -> tuple[int, dict[str, Any]]:
        self._counter += 1
        request_id = self._counter
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        if streaming:
            self._events[request_id] = asyncio.Queue(maxsize=64)
        return request_id, {"jsonrpc": "2.0", "id": request_id}

    async def call(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        await self.start()
        request_id, message = self._new_request()
        message.update(method=method, params=params)
        try:
            await self._write(message)
            result = await asyncio.wait_for(self._pending[request_id], timeout)
            if "error" in result:
                raise failure_from_rpc(result["error"])
            return result.get("result")
        except (TimeoutError, asyncio.CancelledError):
            self._cancel(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def stream(self, method: str, params: dict[str, Any], timeout: float):
        await self.start()
        request_id, message = self._new_request(streaming=True)
        message.update(method=method, params=params)
        queue = self._events[request_id]
        try:
            await self._write(message)
            while True:
                event = await asyncio.wait_for(queue.get(), timeout)
                if event["method"] == "chunk":
                    yield event.get("params", "")
                elif event["method"] == "error":
                    raise failure_from_rpc({"message": event.get("params", {}).get("message", "插件流失败"), "data": event.get("params", {}).get("data")})
                elif event["method"] == "end":
                    if event.get("params", {}).get("error"):
                        raise RuntimeError(event["params"]["error"])
                    break
        except (TimeoutError, asyncio.CancelledError):
            self._cancel(request_id)
            raise
        except (GeneratorExit, StopAsyncIteration):
            self._cancel(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)
            self._events.pop(request_id, None)

    async def close(self) -> None:
        self._closed = True
        if self._reader:
            self._reader.cancel()
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 2)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
