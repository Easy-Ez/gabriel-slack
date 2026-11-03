"""Files: three-step upload, info, delete and guarded private downloads."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, Protocol

from ..core.types import RequestOpts
from ..errors import GabrielError
from ..util.aio import AbortSignal
from ..util.validate import assert_channel_id, assert_file_id, assert_non_empty_string, assert_ts
from .apis import FileInfo, to_file_info
from .hydrator import Hydrator

if TYPE_CHECKING:
    from ..core.core import GabrielCore

#: Bytes, text (a string is file content, never a path) or an async byte stream.
UploadContent = bytes | bytearray | memoryview | str | AsyncIterable[bytes]


class FileLikeRef(Protocol):
    @property
    def url_private(self) -> str: ...


#: A file ID, a private URL, or a :class:`FileRef` / :class:`FileInfo`.
FileLike = str | FileLikeRef

_NON_RETRYABLE_UPLOAD = frozenset(
    {
        "file_uploads_disabled",
        "file_uploads_except_images_disabled",
        "malware_detected",
        "files_upload_has_exceeded_limit",
        "blocked_file_type",
        "file_upload_size_restricted",
        "file_upload_quota_exceeded",
        "dlp_user_warning",
    }
)

_FILE_ID = re.compile(r"^F[A-Z0-9]+$")
_PATH_CHARS = re.compile(r"[\\/\0]")
_SIGNIN = re.compile(r"signin|login", re.IGNORECASE)
_UPLOAD_TIMEOUT = 10 * 60.0


def _s(v: Any) -> str | None:
    return v if isinstance(v, str) and len(v) > 0 else None


def _multipart_body(
    field: str, filename: str, mime: str, body: bytes | AsyncIterable[bytes]
) -> tuple[AsyncIterator[bytes], str]:
    boundary = f"----gabriel{secrets.token_hex(12)}"
    safe_name = re.sub(r'["\r\n]', "_", filename)
    safe_mime = re.sub(r"[\r\n]", "", mime)
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
        f"Content-Type: {safe_mime}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    async def stream() -> AsyncIterator[bytes]:
        yield head
        if isinstance(body, bytes):
            yield body
        else:
            async for chunk in body:
                yield chunk
        yield tail

    return stream(), f"multipart/form-data; boundary={boundary}"


class FilesApi:
    def __init__(
        self,
        core: GabrielCore,
        h: Hydrator,
        on_client_msg_id: Callable[[str], None] = lambda _id: None,
    ) -> None:
        self._core = core
        self._h = h
        #: Registers the share message's client_msg_id so its echo is recognized as this client's.
        self._on_client_msg_id = on_client_msg_id

    async def upload(
        self,
        filename: str,
        content: UploadContent,
        *,
        size: int | None = None,
        title: str | None = None,
        mime_type: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        intro_markdown: str | None = None,
    ) -> FileInfo:
        """Three-step upload: files.getUploadURL → POST bytes → files.completeUpload.

        Shares the file when ``channel`` is given. ``size`` (bytes) is required to stream an async
        iterable without buffering it; ``intro_markdown`` is a plain-text message posted with it.
        """
        filename = assert_non_empty_string(filename, "filename", 255)
        if _PATH_CHARS.search(filename):
            raise GabrielError("INVALID_ARGUMENT", "filename must not contain path separators")
        channel = None if channel is None else assert_channel_id(channel)
        if thread_ts is not None:
            assert_ts(thread_ts, "thread_ts")
        body: bytes | AsyncIterable[bytes]
        if isinstance(content, str):
            body = content.encode("utf-8")
        elif isinstance(content, bytes | bytearray | memoryview):
            body = bytes(content)
        else:
            body = content
        if isinstance(body, bytes):
            length = len(body)
        elif isinstance(size, int) and size >= 0:
            length = size
        else:
            # Unknown length: the protocol needs it up front, so buffer the stream.
            body = b"".join([chunk async for chunk in body])
            length = len(body)

        step1 = await self._core.call_web_api(
            "files.getUploadURL", {"filename": filename, "length": length}, RequestOpts(reason="upload-queue")
        )
        file_id = _s(step1.get("file"))
        upload_url = _s(step1.get("upload_url"))
        if not file_id or not upload_url:
            raise GabrielError("SLACK_API_ERROR", "files.getUploadURL returned no upload URL")

        attempts = 3 if isinstance(body, bytes) else 1
        i = 0
        while True:
            stream, content_type = _multipart_body("file", filename, mime_type or "application/octet-stream", body)
            try:
                resp = await self._core.http.fetch_authenticated(
                    upload_url,
                    method="POST",
                    headers={"Content-Type": content_type},
                    content=stream,
                    timeout=_UPLOAD_TIMEOUT,
                    bearer=False,
                )
                await resp.aclose()
                if resp.is_success:
                    break
                err = GabrielError(
                    "SERVER_ERROR" if resp.status_code >= 500 else "SLACK_API_ERROR",
                    f"file upload failed (HTTP {resp.status_code})",
                )
                if resp.status_code < 500 or i + 1 >= attempts:
                    raise err
            except GabrielError as err:
                if err.code == "SLACK_API_ERROR" or i + 1 >= attempts:
                    raise
            i += 1

        client_msg_id = str(uuid.uuid4())
        self._on_client_msg_id(client_msg_id)
        try:
            fields: dict[str, Any] = {"files": [{"id": file_id, "title": title or filename}]}
            if channel:
                fields["channel"] = channel
            if thread_ts:
                fields["thread_ts"] = thread_ts
            if intro_markdown:
                fields["blocks"] = [
                    {
                        "type": "rich_text",
                        "elements": [
                            {"type": "rich_text_section", "elements": [{"type": "text", "text": intro_markdown}]}
                        ],
                    }
                ]
            fields["client_msg_id"] = client_msg_id
            res = await self._core.call_web_api(
                "files.completeUpload", fields, RequestOpts(reason="upload-queue", retry="none")
            )
            files = res.get("files")
            first = files[0] if isinstance(files, list) and files and isinstance(files[0], dict) else None
            return to_file_info(first or {"id": file_id, "name": filename})
        except GabrielError as err:
            raw = err.raw_slack_error
            if raw and raw in _NON_RETRYABLE_UPLOAD:
                raise GabrielError("PERMISSION_DENIED", f"upload rejected: {raw}", raw_slack_error=raw) from err
            raise

    async def get_info(self, file_id: str) -> FileInfo:
        res = await self._core.call_web_api(
            "files.info", {"file": assert_file_id(file_id)}, RequestOpts(reason="fetching-a-file-object")
        )
        file = res.get("file")
        if not isinstance(file, dict):
            raise GabrielError("SLACK_API_ERROR", "files.info returned no file")
        return to_file_info(file)

    async def delete(self, file_id: str) -> None:
        await self._core.call_web_api(
            "files.delete", {"file": assert_file_id(file_id)}, RequestOpts(reason="delete-file")
        )

    async def download_stream(
        self, file: FileLike, *, max_bytes: int | None = None, signal: AbortSignal | None = None
    ) -> AsyncIterator[bytes]:
        """Streams a private file (cookie + bearer). Refuses sign-in pages; enforces ``max_bytes``."""
        expected_mime: str | None
        if isinstance(file, str):
            if _FILE_ID.match(file):
                info = await self.get_info(file)
                url, expected_mime = info.url_private, info.mime_type
            else:
                url, expected_mime = file, None
        else:
            url = file.url_private
            expected_mime = getattr(file, "mime_type", None)
        if not url:
            raise GabrielError("INVALID_ARGUMENT", "file has no private URL")
        resp = await self._core.http.fetch_authenticated(url, method="GET", signal=signal, stream=True)
        try:
            if resp.status_code == 404:
                raise GabrielError("SLACK_API_ERROR", "file not found", raw_slack_error="file_not_found")
            if not resp.is_success:
                raise GabrielError(
                    "SERVER_ERROR" if resp.status_code >= 500 else "PERMISSION_DENIED",
                    f"file download failed (HTTP {resp.status_code})",
                )
            ctype = resp.headers.get("content-type", "")
            redirected = len(resp.history) > 0
            if (
                "text/html" in ctype
                and expected_mime != "text/html"
                and (redirected or _SIGNIN.search(str(resp.url)) or expected_mime is not None)
            ):
                raise GabrielError(
                    "PERMISSION_DENIED",
                    "download returned an HTML sign-in page; the session cookie / token pair is not accepted",
                )
            declared = resp.headers.get("content-length")
            if max_bytes is not None and declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise GabrielError("FILE_TOO_LARGE", f"file is {declared} bytes (> max_bytes {max_bytes})")
        except BaseException:
            await resp.aclose()
            raise

        async def body() -> AsyncIterator[bytes]:
            seen = 0
            try:
                async for chunk in resp.aiter_bytes():
                    # The signal also cancels the body, like a fetch signal in the reference.
                    if signal is not None:
                        signal.throw_if_aborted()
                    seen += len(chunk)
                    if max_bytes is not None and seen > max_bytes:
                        raise GabrielError("FILE_TOO_LARGE", f"download exceeded max_bytes {max_bytes}")
                    yield chunk
            finally:
                await resp.aclose()

        return body()

    async def download_bytes(
        self, file: FileLike, *, max_bytes: int | None = None, signal: AbortSignal | None = None
    ) -> bytes:
        stream = await self.download_stream(file, max_bytes=max_bytes, signal=signal)
        return b"".join([chunk async for chunk in stream])
