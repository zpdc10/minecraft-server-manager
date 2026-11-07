"""Read one file out of a remote zip (a .mrpack) without downloading the whole archive.

Modpack archives can be hundreds of MB because of their overrides folder, but the
manifest we need is a few KB. With HTTP Range requests we fetch the zip's end-of-central-
directory record, find the manifest's entry, and fetch only its bytes. If the server
ignores Range, we fall back to downloading the archive (up to MAX_FULL_DOWNLOAD).
"""

import io
import struct
import zipfile
import zlib

import httpx

EOCD_SIG = b"PK\x05\x06"
CDIR_SIG = b"PK\x01\x02"
LOCAL_SIG = b"PK\x03\x04"
TAIL_BYTES = 65_536 + 22  # max comment length + EOCD size
MAX_FULL_DOWNLOAD = 300 * 1024 * 1024
MAX_MEMBER_SIZE = 32 * 1024 * 1024


class RemoteZipError(Exception):
    pass


async def _range(client: httpx.AsyncClient, url: str, header: str) -> tuple[bytes, bool]:
    """GET with a Range header. Returns (body, was_partial)."""
    async with client.stream("GET", url, headers={"Range": header}) as resp:
        resp.raise_for_status()
        partial = resp.status_code == 206
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_FULL_DOWNLOAD:
                raise RemoteZipError("archive is too large to download")
            chunks.append(chunk)
        return b"".join(chunks), partial


def _from_full(data: bytes, name: str) -> bytes | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            try:
                return zf.read(name)
            except KeyError:
                return None
    except zipfile.BadZipFile as exc:
        raise RemoteZipError(f"not a zip file: {exc}") from exc


def _find_entry(cdir: bytes, name: str) -> tuple[int, int, int, int] | None:
    """Return (method, compressed size, uncompressed size, local header offset)."""
    pos = 0
    target = name.encode()
    while pos + 46 <= len(cdir) and cdir[pos : pos + 4] == CDIR_SIG:
        (method,) = struct.unpack_from("<H", cdir, pos + 10)
        csize, usize = struct.unpack_from("<II", cdir, pos + 20)
        nlen, elen, clen = struct.unpack_from("<HHH", cdir, pos + 28)
        (offset,) = struct.unpack_from("<I", cdir, pos + 42)
        fname = cdir[pos + 46 : pos + 46 + nlen]
        if fname == target:
            if 0xFFFFFFFF in (csize, usize, offset):
                raise RemoteZipError("zip64 entries are not supported")
            return method, csize, usize, offset
        pos += 46 + nlen + elen + clen
    return None


def _decompress(method: int, data: bytes, usize: int) -> bytes:
    if usize > MAX_MEMBER_SIZE:
        raise RemoteZipError("manifest is too large")
    if method == 0:
        return data
    if method == 8:
        return zlib.decompressobj(-15).decompress(data, MAX_MEMBER_SIZE)
    raise RemoteZipError(f"unsupported compression method {method}")


async def read_member(client: httpx.AsyncClient, url: str, name: str) -> bytes | None:
    """Return the bytes of `name` inside the zip at `url`, or None if it isn't there."""
    tail, partial = await _range(client, url, f"bytes=-{TAIL_BYTES}")
    if not partial:
        return _from_full(tail, name)

    eocd = tail.rfind(EOCD_SIG)
    if eocd < 0 or eocd + 22 > len(tail):
        raise RemoteZipError("end of central directory not found")
    cd_size, cd_offset = struct.unpack_from("<II", tail, eocd + 12)
    if 0xFFFFFFFF in (cd_size, cd_offset):
        raise RemoteZipError("zip64 archives are not supported")

    # The tail usually contains the whole central directory already.
    # Work out where the tail starts in the file: it ends right after EOCD + comment.
    eocd_offset_in_file = cd_offset + cd_size  # EOCD directly follows the central directory
    tail_start = eocd_offset_in_file - eocd
    if tail_start <= cd_offset:
        start = cd_offset - tail_start
        cdir = tail[start : start + cd_size]
    else:
        cdir, _ = await _range(client, url, f"bytes={cd_offset}-{cd_offset + cd_size - 1}")

    entry = _find_entry(cdir, name)
    if entry is None:
        return None
    method, csize, usize, offset = entry

    # Local header is 30 bytes + name + extra; the extra length can differ from the
    # central directory's, so read the header first, then the data.
    header, _ = await _range(client, url, f"bytes={offset}-{offset + 29}")
    if header[:4] != LOCAL_SIG:
        raise RemoteZipError("bad local file header")
    nlen, elen = struct.unpack_from("<HH", header, 26)
    data_start = offset + 30 + nlen + elen
    if csize == 0:
        return _decompress(method, b"", usize)
    data, _ = await _range(client, url, f"bytes={data_start}-{data_start + csize - 1}")
    return _decompress(method, data, usize)
