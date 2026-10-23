#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
#   "refkv[zarr]>=0.1.0",
#   "zarr-http-server",
# ]
# ///
"""Serve every image file under a directory as a read-only Zarr v3 store over HTTP.

refkv (https://github.com/d-v-b/refkv) reads an image file (e.g. SVS, NDPI, OME-TIFF,
QPTIFF, CZI) and builds an *index* of it: a Zarr hierarchy (OME-Zarr
multiscales metadata, plus refkv's own) whose chunks are references to byte
ranges in the original file. This script serves that hierarchy, exactly as refkv
wrote it, with zarr-http-server. Nothing is decoded or re-encoded: each chunk is
read from the file and sent as stored (e.g. a complete JPEG tile, codec
"imagecodecs_jpeg"), so clients need the matching Zarr codecs (Python:
`pip install imagecodecs-zarr`).

    uv run serve_refkv_minimal.py /path/to/images -i /path/to/index-cache

Each option can also be set by an environment variable, REFKV_SERVE_<OPTION>, e.g.
REFKV_SERVE_ROOT or REFKV_SERVE_INDEX_DIR (--help lists them); a flag on the command
line wins.

    GET /<path/to/image.svs>/zarr.json     -> root metadata, as refkv wrote it
    GET /<path/to/image.svs>/0/c/0/0/0     -> one chunk, as stored in the file
    GET /_status                           -> the images currently open (--no-status hides it)

Each image is also served at /<path>.zarr, since some clients (napari-ome-zarr)
only recognize URLs ending in ".zarr".

Which files are served is up to the installed refkv: any file under the directory
that refkv can read. A file refkv rejects gets the same 404 as a missing file.

Indexes: an image file is indexed on its first request, and the index is saved as
<path>.vzip in --index-dir. The image files are assumed never to change, so a saved
index is used whenever it exists (after upgrading refkv, empty the index directory
to index the files again). Without --index-dir, indexes are kept in a temporary
directory removed when the server stops. Up to --max-open images are kept open
(least recently used are evicted first).

Storage: the image files are read through the filesystem, so the directory can be a
mount of network storage, such as NFS or a bucket mounted read-only with gcsfuse.
--max-reads is how many requests are read from storage at once, across all images;
on network storage, where each read waits on the network, raise it until throughput
stops improving.

Everything refkv writes is served, including `refkv_source`, the file's original
metadata (much as bioformats2raw keeps it in OME/METADATA.ome.xml). Keep the
index directory private, like the image files themselves.
"""

import asyncio
import contextlib
import logging
import pathlib
import signal
import tempfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import click
import refkv.zarr
from refkv.virtualize import Rejected, virtualize
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from zarr_http_server import serve, store_app

log = logging.getLogger("refkv-minimal")

ZARR_URL_SUFFIX = ".zarr"
INDEX_SUFFIX = ".vzip"


class ImageRequest(NamedTuple):
    """A request URL, split into the image file it names and the Zarr key within it."""

    image: str   # the image file's path under the root, e.g. "a/image.svs"
    prefix: str  # the URL path that named it, e.g. "a/image.svs.zarr"
    key: str     # the Zarr key, e.g. "0/c/0/1/2"


class ImageRouter:
    """ASGI app that sends each request for /<image file>/<key> to that image's Zarr store app,
    opening (and if needed indexing) the file on its first request."""

    def __init__(self, root: pathlib.Path, index_dir: pathlib.Path, max_open: int, max_reads: int):
        self.root = root.resolve()
        self.index_dir = index_dir.resolve()
        self.max_open = max_open
        self.max_reads = max_reads
        self._reads = asyncio.Semaphore(max_reads)  # requests reading from storage, all images
        self.open_images: OrderedDict[str, Starlette] = OrderedDict()  # least -> most recently used
        self._opening: dict[str, asyncio.Task] = {}  # images being opened, shared by their requests

    def parse_request_path(self, url_path: str) -> ImageRequest | None:
        """Split a URL path at the file it names under the root:
        '/a/image.svs.zarr/0/c/0/1/2' -> ImageRequest('a/image.svs', 'a/image.svs.zarr', '0/c/0/1/2').
        None if no prefix of the path is a file under the root."""
        parts = url_path.strip("/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            return None
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            image = prefix.removesuffix(ZARR_URL_SUFFIX)
            path = self.root / image
            if image in self.open_images or path.is_file():
                if not path.resolve().is_relative_to(self.root):  # a symlink out of the root
                    return None
                return ImageRequest(image, prefix, "/".join(parts[i:]))
            if not (self.root / prefix).is_dir():
                return None
        return None

    def load_index(self, image: str) -> refkv.zarr.ZarrStore:
        """The image file's refkv index as a read-only Zarr store: from the index directory
        if it is there, else built now and saved there. The image files are assumed never
        to change, so a saved index is not checked against its file.
        Raises refkv's Rejected for a file refkv can't read."""
        path = (self.root / image).resolve()
        vzip = self.index_dir / f"{image}{INDEX_SUFFIX}"
        if not vzip.is_file():
            log.info("indexing %s -> %s", path, vzip)
            vzip.parent.mkdir(parents=True, exist_ok=True)
            partial = vzip.with_name(vzip.name + ".partial")
            virtualize(str(path)).save(str(partial))
            partial.replace(vzip)  # so a reader never sees a half-written index
        # no per-image cap on reads in flight: the router caps them across all images
        return refkv.zarr.open(str(vzip), max_concurrency=None)

    def make_image_app(self, image: str) -> Starlette:
        """The zarr-http-server app serving the image file's index. Blocking: indexing reads
        the file and can take seconds."""
        # store_app serves every key of the store as stored, without parsing the arrays,
        # so any codec or chunk grid refkv writes is passed through to the client
        return store_app(self.load_index(image))

    async def get_image_app(self, image: str) -> Starlette:
        """The image's app: the open one, or opened now. Concurrent requests for an image
        that is being opened wait for the same open."""
        if image in self.open_images:
            self.open_images.move_to_end(image)
            return self.open_images[image]
        if image not in self._opening:
            # a task of its own, so a client disconnecting mid-open doesn't cancel it for the rest
            self._opening[image] = asyncio.create_task(self._open_image(image))
        return await asyncio.shield(self._opening[image])

    async def _open_image(self, image: str) -> Starlette:
        """Make the image's app in a worker thread, then keep it open, evicting the least
        recently used images beyond max_open. An evicted image has nothing to close: it is
        dropped, and freed once requests still using it finish."""
        try:
            app = await asyncio.to_thread(self.make_image_app, image)
        finally:
            del self._opening[image]  # a failed open is tried again on the next request
        self.open_images[image] = app
        while len(self.open_images) > self.max_open:
            log.info("evicting %s", self.open_images.popitem(last=False)[0])
        return app

    async def __call__(self, scope, receive, send):
        # the path below this app's mount point (the route's {path}), not scope["path"],
        # which also holds root_path when the app is mounted under a prefix
        request = self.parse_request_path(scope["path_params"]["path"])
        if request is None:
            return await PlainTextResponse("Not found", 404)(scope, receive, send)
        try:
            app = await self.get_image_app(request.image)
        except Rejected:  # not a format refkv reads: answered like a missing file
            return await PlainTextResponse("Not found", 404)(scope, receive, send)
        except Exception as exc:
            log.warning("could not open %s: %s", request.image, exc)
            return await PlainTextResponse(f"Could not open {request.image}", 500)(scope, receive, send)
        # pass the request on as if the image's app were mounted at /<prefix>: Starlette
        # routes on scope["path"] with root_path removed, which leaves /<key>
        mount = scope.get("root_path", "") + "/" + request.prefix
        async with self._reads:
            await app(dict(scope, root_path=mount), receive, send)


def build_app(root: pathlib.Path, index_dir: pathlib.Path, max_open: int, max_reads: int = 32,
              status: bool = True) -> Starlette:
    """The server's ASGI app: the image router, /_status, and CORS for browser clients.

    max_reads caps the requests reading from storage at once, across all images. refkv
    may read files in the event loop's default thread pool, so the pool is sized to match
    at startup. Startup runs only when this is the top-level app: an app that mounts this
    one must size its own loop's pool."""
    router = ImageRouter(root, index_dir, max_open, max_reads)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_reads, thread_name_prefix="read"))
        yield

    async def status_page(request):
        return JSONResponse({"root": str(router.root),
                             "index_dir": str(router.index_dir),
                             "max_open": router.max_open,
                             "max_reads": router.max_reads,
                             "open": list(router.open_images)})  # least -> most recently used

    routes = [Route("/{path:path}", router, methods=["GET", "HEAD"])]
    if status:
        routes.insert(0, Route("/_status", status_page, methods=["GET"]))
    # CORS on the outer app, so preflights and 404s get the headers without opening an image
    cors = Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "HEAD"],
                      allow_headers=["Range"], expose_headers=["Content-Range"])
    return Starlette(routes=routes, middleware=[cors], lifespan=lifespan)


@click.command(context_settings={"auto_envvar_prefix": "REFKV_SERVE", "show_default": True})
@click.argument("root", envvar="REFKV_SERVE_ROOT",
                type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path))
@click.option("-i", "--index-dir", type=click.Path(file_okay=False, path_type=pathlib.Path),
              show_envvar=True,
              help="Keep each image file's .vzip index here, private "
                   "(default: a temporary directory, removed on exit).")
@click.option("-p", "--port", default=9876, show_envvar=True)
@click.option("--host", default="0.0.0.0", show_envvar=True)
@click.option("-n", "--max-open", default=32, show_envvar=True,
              help="Images kept open at once (least recently used are evicted).")
@click.option("--max-reads", default=32, type=click.IntRange(min=1), show_envvar=True,
              help="Requests read from storage at once, across all images. Raise it for "
                   "network storage such as NFS or gcsfuse.")
@click.option("--status/--no-status", default=True, show_envvar=True,
              help="Serve /_status, which lists open images' file names.")
def main(root, index_dir, port, host, max_open, max_reads, status):
    """Serve the image files under ROOT (or $REFKV_SERVE_ROOT) as read-only Zarr v3
    stores, indexed by refkv, with chunks sent as stored in the files."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # uvicorn shuts down cleanly, then re-raises the signal that stopped it. Raised as
    # KeyboardInterrupt (as Ctrl-C is), SIGTERM still lets the temporary directory be removed.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        with tempfile.TemporaryDirectory(prefix="refkv-index-") as tmp:
            app = build_app(root, index_dir or pathlib.Path(tmp), max_open, max_reads, status)
            serve(app, host=host, port=port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
