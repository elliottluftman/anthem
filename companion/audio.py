"""Audio file helpers for renders (stdlib only).

Live records AIFF or WAV (16/24/32-bit int or 32-bit float, user preference). A render is
shipped as PCM WAV: 16-bit sources stay 16-bit, everything else becomes 24-bit. Python 3.13
removed ``aifc``, so AIFF/AIFC is parsed here directly.
"""
import hashlib
import struct
import wave

from anthem_core import protocol


class AudioError(ValueError):
    pass


def _read_chunks(data, big_endian):
    """Yield (id, payload) for an IFF/RIFF container body (after the 12-byte header)."""
    pos = 12
    fmt = ">I" if big_endian else "<I"
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack(fmt, data[pos + 4:pos + 8])[0]
        yield cid, data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)


def _ext80_to_float(b):
    """IEEE 754 80-bit extended -> float (AIFF sample rate)."""
    exp = ((b[0] & 0x7F) << 8) | b[1]
    mant = struct.unpack(">Q", b[2:10])[0]
    if exp == 0 and mant == 0:
        return 0.0
    return mant * 2.0 ** (exp - 16383 - 63)


def read_pcm(path):
    """Read an AIFF/AIFC or WAV file. Returns (sample_rate, channels, bits, pcm_le_bytes) where
    bits is 16 or 24 and the samples are little-endian signed PCM, interleaved."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 12:
        raise AudioError("file too short")
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return _read_aiff(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return _read_wav(data)
    raise AudioError("not an AIFF or WAV file (set Live's recording file type to WAV or AIFF)")


def _read_aiff(data):
    comm = ssnd = None
    for cid, body in _read_chunks(data, True):
        if cid == b"COMM":
            comm = body
        elif cid == b"SSND":
            ssnd = body
    if comm is None or ssnd is None:
        raise AudioError("AIFF without COMM/SSND")
    channels, frames, bits = struct.unpack(">hLh", comm[:8])
    rate = int(round(_ext80_to_float(comm[8:18])))
    comp = comm[18:22] if len(comm) >= 22 else b"NONE"
    offset = struct.unpack(">L", ssnd[:4])[0]
    raw = ssnd[8 + offset:]
    if comp in (b"NONE", b"twos") or len(comm) < 22:
        return rate, channels, bits, _int_be_to_le(raw, bits, channels, frames)
    if comp in (b"fl32", b"FL32"):
        return rate, channels, 24, _float_to_24(raw, ">f", channels, frames)
    if comp == b"sowt":   # little-endian ints (rare in AIFC)
        return rate, channels, 16 if bits == 16 else 24, _int_le_to_le(raw, bits, channels, frames)
    raise AudioError("unsupported AIFF compression %r" % comp)


def _read_wav(data):
    fmt = None
    raw = None
    for cid, body in _read_chunks(data, False):
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            raw = body
    if fmt is None or raw is None:
        raise AudioError("WAV without fmt/data")
    tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == 0xFFFE and len(fmt) >= 26:      # WAVE_FORMAT_EXTENSIBLE: sub-format GUID starts with the tag
        tag = struct.unpack("<H", fmt[24:26])[0]
    frames = len(raw) // (channels * (bits // 8))
    if tag == 1:
        return rate, channels, 16 if bits == 16 else 24, _int_le_to_le(raw, bits, channels, frames)
    if tag == 3 and bits == 32:
        return rate, channels, 24, _float_to_24(raw, "<f", channels, frames)
    raise AudioError("unsupported WAV format tag %d / %d bits" % (tag, bits))


def _frames(raw, bits, channels, frames):
    bps = bits // 8
    n = min(frames * channels, len(raw) // bps)
    return raw[:n * bps], n, bps


def _int_be_to_le(raw, bits, channels, frames):
    raw, n, bps = _frames(raw, bits, channels, frames)
    if bits == 16:
        return _swap(raw, 2)
    if bits == 24:
        return _swap(raw, 3)
    if bits == 32:
        # keep the top 24 bits: big-endian b0 b1 b2 b3 -> little-endian b2 b1 b0
        out = bytearray(n * 3)
        out[0::3] = raw[2::4]
        out[1::3] = raw[1::4]
        out[2::3] = raw[0::4]
        return bytes(out)
    raise AudioError("unsupported sample size %d" % bits)


def _int_le_to_le(raw, bits, channels, frames):
    raw, n, bps = _frames(raw, bits, channels, frames)
    if bits in (16, 24):
        return raw
    if bits == 32:
        out = bytearray(n * 3)
        out[0::3] = raw[1::4]
        out[1::3] = raw[2::4]
        out[2::3] = raw[3::4]
        return bytes(out)
    raise AudioError("unsupported sample size %d" % bits)


def _swap(raw, width):
    out = bytearray(len(raw))
    for i in range(width):
        out[i::width] = raw[width - 1 - i::width]
    return bytes(out)


def _float_to_24(raw, fmt, channels, frames):
    raw, n, _ = _frames(raw, 32, channels, frames)
    vals = struct.unpack(fmt[0] + "%df" % n, raw)
    out = bytearray(n * 3)
    full = 8388607.0
    i = 0
    for v in vals:
        if v > 1.0:
            v = 1.0
        elif v < -1.0:
            v = -1.0
        s = int(round(v * full)) & 0xFFFFFF
        out[i] = s & 0xFF
        out[i + 1] = (s >> 8) & 0xFF
        out[i + 2] = (s >> 16) & 0xFF
        i += 3
    return bytes(out)


def write_wav(path, rate, channels, bits, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(pcm)


def to_wav(src, dst):
    """Convert a Live recording to a shippable WAV. Returns the render metadata (without
    render_id/beats): sample_rate, channels, bits, size, sha256, n_chunks."""
    rate, channels, bits, pcm = read_pcm(src)
    if channels not in (1, 2):
        raise AudioError("%d channels not supported" % channels)
    if not pcm:
        raise AudioError("no audio data (recording still being written?)")
    write_wav(dst, rate, channels, bits, pcm)
    return describe(dst, rate, channels, bits)


def describe(path, rate, channels, bits):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) > protocol.MAX_BLOB_BYTES:
        raise AudioError("render is %d MiB; the limit is %d MiB" % (
            len(data) // 1048576, protocol.MAX_BLOB_BYTES // 1048576))
    return {"sample_rate": rate, "channels": channels, "bits": bits, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "n_chunks": -(-len(data) // protocol.BLOB_CHUNK_BYTES)}


def read_chunk(path, idx):
    with open(path, "rb") as f:
        f.seek(idx * protocol.BLOB_CHUNK_BYTES)
        return f.read(protocol.BLOB_CHUNK_BYTES)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
