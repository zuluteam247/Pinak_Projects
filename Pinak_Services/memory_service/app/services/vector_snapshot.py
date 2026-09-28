"""Strict, non-pickle vector snapshots. Legacy .npy needs offline migration."""
import os
import stat
import zipfile
import zlib

import numpy as np

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
MAX_ROWS = 30_000
MAX_DIMENSION = 4096
MAX_ARRAY_BYTES = 60 * 1024 * 1024
MEMBERS = {"format_version.npy", "vectors.npy", "ids.npy"}


def _member(archive, info, expected_dtype, shape_check):
    # Parse the NPY header and bounds before allocating a NumPy array.
    with archive.open(info) as member:
        major, minor = np.lib.format.read_magic(member)
        if (major, minor) == (1, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_1_0(member)
        elif (major, minor) == (2, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_2_0(member)
        else:
            raise ValueError("Unsupported array version")
        if fortran or dtype != np.dtype(expected_dtype) or not shape_check(shape):
            raise ValueError("Invalid vector snapshot array header")
        count = 1
        for axis in shape:
            count *= axis
        byte_count = count * dtype.itemsize
        if byte_count > MAX_ARRAY_BYTES or info.file_size != member.tell() + byte_count:
            raise ValueError("Invalid vector snapshot member size")
        with archive.open(info) as payload:
            return np.load(payload, allow_pickle=False)


def read_snapshot(path, dimension=None):
    if dimension is not None and not (1 <= dimension <= MAX_DIMENSION):
        raise ValueError("Invalid configured vector dimension")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            stat_result = os.fstat(source.fileno())
            # Detect ordinary in-place replacements during parsing; not an
            # authentication mechanism against a same-user malicious writer.
            if not stat.S_ISREG(stat_result.st_mode) or not 0 < stat_result.st_size <= MAX_FILE_BYTES:
                raise ValueError("Invalid vector snapshot file size/type")
            if source.read(4) != b"PK\x03\x04":
                raise ValueError("Legacy or unsupported vector snapshot; manual migration required")
            source.seek(0)
            with zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
                if (len(infos) != 3 or {info.filename for info in infos} != MEMBERS
                        or len({info.filename for info in infos}) != len(infos)):
                    raise ValueError("Invalid vector snapshot members")
                if any(info.flag_bits & 1 or info.compress_type != zipfile.ZIP_STORED
                       or info.is_dir() or info.file_size > MAX_ARRAY_BYTES
                       or info.compress_size != info.file_size for info in infos):
                    raise ValueError("Unsupported vector snapshot member encoding or size")
                if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
                    raise ValueError("Vector snapshot total allocation exceeds limit")
                by_name = {info.filename: info for info in infos}
                version = _member(archive, by_name["format_version.npy"], "int64", lambda shape: shape == ())
                vectors = _member(archive, by_name["vectors.npy"], "float32",
                                  lambda shape: len(shape) == 2 and shape[0] <= MAX_ROWS
                                  and 1 <= shape[1] <= MAX_DIMENSION
                                  and (dimension is None or shape[1] == dimension))
                ids = _member(archive, by_name["ids.npy"], "int64",
                              lambda shape: len(shape) == 1 and shape[0] <= MAX_ROWS
                              and shape[0] == len(vectors))
                if int(version) != 1 or not np.isfinite(vectors).all() or (ids < 0).any():
                    raise ValueError("Invalid vector snapshot version or values")
                # Duplicates are accepted only so startup reconciliation can
                # re-encode them from the canonical DB; negative IDs are not.
                after = os.fstat(source.fileno())
                if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        != (stat_result.st_dev, stat_result.st_ino, stat_result.st_size,
                            stat_result.st_mtime_ns, stat_result.st_ctime_ns)):
                    raise ValueError("Vector snapshot changed while reading")
                return vectors, ids
    except (OSError, EOFError, KeyError, TypeError, zipfile.BadZipFile, zlib.error, OverflowError, MemoryError) as exc:
        raise ValueError("Invalid vector snapshot; manual recovery required") from exc
