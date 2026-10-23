# refkv-server

`serve_refkv_minimal.py` serves every image file under a directory as a read-only
Zarr v3 store over HTTP.

It uses [refkv](https://github.com/d-v-b/refkv) to index image files (SVS, NDPI,
OME-TIFF, QPTIFF, CZI, and any other format refkv reads) on-demand if indexes
don't exist. The index is a Zarr hierarchy with OME-Zarr multiscales metadata, 
whose chunks point to byte ranges in the original file. The script serves that 
index with [zarr-http-server](https://github.com/zarr-developers/zarr-python/tree/main/packages/zarr-http-server).

Note: each chunk is read from the image file and sent as stored, for
example as a complete JPEG tile. Clients need the matching Zarr codecs; in Python,
install `imagecodecs-zarr`.

This script started as a hand-rolled proof of concept that was further developed
and tested with use of Claude Opus 5.5.

## Running

The script declares its own dependencies, so it runs with
[uv](https://docs.astral.sh/uv/) and nothing else installed:

```sh
uv run serve_refkv_minimal.py /path/to/images -i /path/to/index-cache
```

The server listens on port 9876 by default. Run with `--help` for all options.

## URLs

For an image at `/path/to/images/a/image.svs`:

```
GET /a/image.svs/zarr.json      root metadata
GET /a/image.svs/0/c/0/0/0      one chunk, as stored in the file
GET /a/image.svs.zarr/...       the same store, for clients that need a .zarr URL
GET /_status                    the images currently open
```

A file that refkv cannot read returns 404, the same as a missing file.

## Indexes

An image is indexed on its first request, and the index is saved as `<path>.vzip` in
the `--index-dir` directory. A saved index is reused whenever it exists; the
image files are assumed never to change.

Without `--index-dir`, indexes are kept in a temporary directory that is removed when
the server stops.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `ROOT` | | Directory of image files to serve |
| `-i`, `--index-dir` | temporary directory | Where indexes are saved |
| `-p`, `--port` | 9876 | Port to listen on |
| `--host` | 0.0.0.0 | Address to listen on |
| `-n`, `--max-open` | 32 | Images kept open at once; least recently used are closed first |
| `--max-reads` | 32 | Requests read from storage at once, across all images |
| `--status/--no-status` | `--status` | Serve `/_status` |

Each option can also be set with an environment variable named `REFKV_SERVE_<OPTION>`,
for example `REFKV_SERVE_ROOT` or `REFKV_SERVE_INDEX_DIR`. A command-line flag takes
precedence.

## Docker

The `Dockerfile` builds an image for Google Cloud Run. It expects the images mounted
at `/images` and an index directory at `/index`, and listens on `$PORT` (8080 by
default).
