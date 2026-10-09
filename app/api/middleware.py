from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.services.validation import UploadTooLargeError

# Room for the multipart boundaries and part headers around the file itself.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


class MaxBodySizeMiddleware:
    """Reject request bodies over the upload limit before they are fully received.

    FastAPI parses the whole multipart form before the route runs, so without
    this a multi-GB upload would be received and spooled to disk before the
    route's own size check could reject it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = settings.max_upload_size_mb * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES

        # Fast path: the client announced its size up front.
        content_length = Headers(scope=scope).get("content-length", "")
        if content_length.isdigit() and int(content_length) > max_bytes:
            error = UploadTooLargeError()
            response = JSONResponse({"detail": error.message}, status_code=error.status_code)
            await response(scope, receive, send)
            return

        # Chunked uploads have no Content-Length, so count bytes as they arrive.
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    error = UploadTooLargeError()
                    # FastAPI re-raises HTTPExceptions from body parsing as-is.
                    raise HTTPException(status_code=error.status_code, detail=error.message)
            return message

        await self.app(scope, limited_receive, send)
