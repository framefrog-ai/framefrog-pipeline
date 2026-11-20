# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""3D LUT handling: built-in looks, generation, and manifest fingerprints.

FrameFrog ships a small set of looks as Adobe ``.cube`` files. They are
generated deterministically from code (so they can be regenerated and diffed)
and then applied by FFmpeg's ``lut3d`` filter on the GPU box.

If a user supplies ``options["lut"]`` as a path to an existing ``.cube`` file,
that file wins; otherwise the value is looked up as a built-in look name.
"""

from __future__ import annotations

import hashlib
import os
import tempfile

LUT_SIZE = 17

#: Built-in look names -> human description. The math lives in :func:`_look`.
BUILTIN_LOOKS: dict[str, str] = {
    "cinematic_warm": "Warm highlights, gently cooled shadows, filmic contrast",
    "teal_orange": "Split-tone: teal shadows, orange skin tones",
    "neutral_film": "Clean filmic contrast with a mild saturation lift",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_APP_ROOT)

#: Where a *committed* look lives (checked first, never written to).
_DEFAULT_DIRS = [
    os.path.join(_REPO_ROOT, "assets", "luts"),
    os.path.join(_HERE, "assets", "luts"),
]


def _runtime_dirs() -> list[str]:
    """Writable directories for looks that have to be generated.

    Generated looks never land in the source tree: they go to the model
    cache, the work directory or the system temp directory.
    """
    import tempfile

    dirs: list[str] = []
    model_cache = os.environ.get("MODEL_CACHE_DIR")
    if model_cache:
        dirs.append(os.path.join(model_cache, "luts"))
    work_dir = os.environ.get("WORK_DIR")
    if work_dir:
        dirs.append(os.path.join(work_dir, "luts"))
    dirs.append(os.path.join(tempfile.gettempdir(), "framefrog-luts"))
    return dirs


def _clamp(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _s_curve(x: float, contrast: float = 0.18) -> float:
    """Gentle filmic S-curve around 0.5."""
    return _clamp(0.5 + (x - 0.5) * (1.0 + contrast) - contrast * (x - 0.5) ** 3 * 4)


def _sat(r: float, g: float, b: float, amount: float) -> tuple[float, float, float]:
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return (
        _clamp(luma + (r - luma) * amount),
        _clamp(luma + (g - luma) * amount),
        _clamp(luma + (b - luma) * amount),
    )


def _look(name: str, r: float, g: float, b: float) -> tuple[float, float, float]:
    """Evaluate one built-in look at an input RGB (0..1)."""
    if name == "cinematic_warm":
        # filmic contrast, lift shadows a touch, cool the shadows / warm the highs
        out = [_s_curve(_clamp(c + 0.012), 0.14) for c in (r, g, b)]
        out = list(_sat(out[0], out[1], out[2], 0.92))
        shadow = (1.0 - (r + g + b) / 3.0) ** 2
        high = ((r + g + b) / 3.0) ** 2
        out[0] = _clamp(out[0] + 0.05 * high - 0.012 * shadow)
        out[1] = _clamp(out[1] + 0.02 * high)
        out[2] = _clamp(out[2] - 0.05 * high + 0.030 * shadow)
        return tuple(out)  # type: ignore[return-value]

    if name == "teal_orange":
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        shadow = (1.0 - luma) ** 1.6
        high = luma**1.4
        out = [
            _clamp(_s_curve(r, 0.10) + 0.07 * high - 0.05 * shadow),
            _clamp(_s_curve(g, 0.10) + 0.02 * high - 0.01 * shadow),
            _clamp(_s_curve(b, 0.10) - 0.07 * high + 0.07 * shadow),
        ]
        return _sat(out[0], out[1], out[2], 1.06)

    # neutral_film (and anything unknown falls back here)
    out = [_s_curve(_clamp(c), 0.12) for c in (r, g, b)]
    return _sat(out[0], out[1], out[2], 1.02)


_CACHE: dict[str, str] = {}


def _candidate_dirs() -> list[str]:
    """Every directory a look may be read from, most specific first."""
    dirs = list(_DEFAULT_DIRS)
    env = os.environ.get("FRAMEFROG_LUT_DIR")
    if env:
        dirs.insert(0, env)
    return dirs


def _writable_dirs() -> list[str]:
    """Directories a generated look may be written to."""
    dirs = _runtime_dirs()
    env = os.environ.get("FRAMEFROG_LUT_DIR")
    if env:
        dirs.insert(0, env)
    return dirs


def ensure_lut(name_or_path: str) -> str:
    """Return a filesystem path to a usable ``.cube`` LUT.

    * existing file -> returned unchanged
    * built-in look -> generated (once) into the first writable asset dir
    * unknown name -> ValueError listing the built-ins
    """
    if os.path.exists(name_or_path):
        return os.path.abspath(name_or_path)

    name = name_or_path.strip()
    if name.endswith(".cube"):
        name = os.path.splitext(os.path.basename(name))[0]

    if name in _CACHE and os.path.exists(_CACHE[name]):
        return _CACHE[name]

    if name not in BUILTIN_LOOKS:
        raise ValueError(f"unknown LUT {name_or_path!r}; built-ins: {', '.join(sorted(BUILTIN_LOOKS))}")

    for d in _candidate_dirs():
        path = os.path.join(d, f"{name}.cube")
        if os.path.exists(path):
            _CACHE[name] = path
            return path

    target_dir = next(
        (d for d in _writable_dirs() if _writable_dir(d)),
        os.path.join(tempfile.gettempdir(), "framefrog-luts"),
    )
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{name}.cube")
    write_lut(name, path)
    _CACHE[name] = path
    return path


def _writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def write_lut(name: str, path: str, size: int = LUT_SIZE) -> str:
    """Generate ``name`` at ``size`` and write it as an Adobe .cube file.

    .cube ordering is red-fastest: index = r + g*N + b*N*N with r varying
    fastest across lines, which is what FFmpeg's ``lut3d`` expects.
    """
    lines = [
        f'TITLE "{name} (FrameFrog)"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
        "",
    ]
    for b_i in range(size):
        b = b_i / (size - 1)
        for g_i in range(size):
            g = g_i / (size - 1)
            for r_i in range(size):
                r = r_i / (size - 1)
                rr, gg, bb = _look(name, r, g, b)
                lines.append(f"{rr:.6f} {gg:.6f} {bb:.6f}")
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def lut_fingerprint(path: str) -> dict[str, object]:
    """Small manifest record: name, size, sha256 so a job is reproducible."""
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    size = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.upper().startswith("LUT_3D_SIZE"):
                    size = int(line.split()[1])
                    break
    except OSError:
        pass
    return {
        "path": os.path.basename(path),
        "size": size,
        "sha256": digest,
    }


#: A .cube file larger than this is not a LUT, it is a mistake.
MAX_LUT_BYTES = 8 * 1024 * 1024


def validate_lut(path: str) -> dict[str, object]:
    """Validate a ``.cube`` LUT file and return its fingerprint.

    Checks the extension, the size, and the structure (``LUT_3D_SIZE`` plus a
    plausible number of data rows). Raises :class:`ValueError` with a precise
    reason when the file cannot be used.
    """
    import os

    if not str(path).lower().endswith(".cube"):
        raise ValueError("LUT files must use the .cube extension")
    if not os.path.exists(path):
        raise ValueError(f"LUT file does not exist: {path}")
    size = os.path.getsize(path)
    if size <= 0:
        raise ValueError("LUT file is empty")
    if size > MAX_LUT_BYTES:
        raise ValueError(f"LUT file is too large ({size} bytes > {MAX_LUT_BYTES}); refusing to read it")

    lut_size = None
    rows = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            upper = stripped.upper()
            if upper.startswith("LUT_3D_SIZE"):
                try:
                    lut_size = int(stripped.split()[1])
                except (IndexError, ValueError) as exc:
                    raise ValueError("LUT_3D_SIZE is malformed") from exc
                continue
            if upper.startswith(("TITLE", "DOMAIN_MIN", "DOMAIN_MAX", "LUT_1D_SIZE")):
                continue
            fields = stripped.split()
            if len(fields) != 3:
                raise ValueError(f"unexpected LUT data row: {stripped[:40]}")
            try:
                values = [float(v) for v in fields]
            except ValueError as exc:
                raise ValueError(f"non-numeric LUT data row: {stripped[:40]}") from exc
            if any(v < -0.001 or v > 1.001 for v in values):
                raise ValueError(f"LUT data row out of range: {stripped[:40]}")
            rows += 1

    if lut_size is None:
        raise ValueError("LUT file has no LUT_3D_SIZE declaration")
    expected = lut_size**3
    if rows != expected:
        raise ValueError(f"LUT declares {lut_size}^3 = {expected} entries but contains {rows}")
    fingerprint = lut_fingerprint(path)
    fingerprint["size"] = lut_size
    fingerprint["entries"] = rows
    return fingerprint
