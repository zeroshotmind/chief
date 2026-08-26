"""Reading the file an artifact names — the one place Chief touches the filesystem.

CONTRACT-NOTES.md #29 refused ``GET /files?path=``, and that refusal stands: a path
parameter is a traversal surface on a process with no auth, and containment would have to be
invented and then defended forever. What is here instead is narrower in a way that removes
the problem rather than guarding it.

**The client never supplies a path.** It names a run and an artifact, both ids Chief issued.
The path comes from the artifact's own ``ref``, resolved against the ``origin_dir`` the
harness recorded on the workflow (#32). The set of readable files is therefore exactly the
set a harness has already reported — nothing else on the disk is addressable, because there
is no way to ask for it.

That also settles the question a startup flag could not answer. A root named at launch is
wrong the moment a second workflow runs somewhere else, and every workflow here carries its
own.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import Conflict, NotFound, ValidationFailed

#: Big enough for the write-ups, logs and screenshots a run produces; small enough that a
#: mis-aimed ref at a database file or a model checkpoint is refused rather than read into
#: memory and pushed down a tunnel.
MAX_BYTES = 25 * 1024 * 1024

#: What the browser is told it may render, keyed by the extension. Everything not named here
#: is a download, never a rendered document — see ``media_type``.
RENDERABLE = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    # Its own type rather than markdown's: the viewer renders the prose either way, but only
    # an MDX file has components and imports in it to be named as such. Not an IANA type —
    # nothing puts it on the wire, since the response body is always octet-stream.
    ".mdx": "text/mdx",
    ".log": "text/plain", ".json": "application/json", ".yaml": "text/plain",
    ".yml": "text/plain", ".toml": "text/plain", ".csv": "text/csv", ".tsv": "text/csv",
    ".py": "text/plain", ".js": "text/plain", ".ts": "text/plain", ".sh": "text/plain",
    ".sql": "text/plain", ".rs": "text/plain", ".go": "text/plain", ".java": "text/plain",
    ".c": "text/plain", ".h": "text/plain", ".cpp": "text/plain", ".css": "text/plain",
    ".ini": "text/plain", ".cfg": "text/plain", ".diff": "text/plain", ".patch": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".avif": "image/avif",
    ".pdf": "application/pdf",
    # The real IANA type, unlike `.mdx` above, and safely so: the response body is always
    # octet-stream regardless of what is named here (see `artifact_content`), so this never
    # controls what hits the wire — only what the UI is told it may render, and it renders
    # HTML in a sandboxed frame with no `allow-same-origin`, never inline on Chief's own
    # origin. See `viewerBody` in app.js.
    ".html": "text/html", ".htm": "text/html",
}


@dataclass(frozen=True)
class ArtifactFile:
    """One file, read whole, with what the browser needs to decide how to show it."""

    path: Path
    data: bytes
    media_type: str
    name: str

    @property
    def size(self) -> int:
        return len(self.data)


def media_type(path: Path) -> str:
    """What this file may be rendered as.

    An allowlist, not a guess. ``mimetypes`` would happily return ``image/svg+xml`` for an
    ``.svg`` artifact, and SVG can carry a ``<script>`` same as HTML can — this list is what
    keeps that a download rather than something the browser executes. HTML *is* on the
    list, deliberately: unlike serving it same-origin, the UI renders it in a sandboxed
    frame with no ``allow-same-origin``, so a script in it can run but cannot read Chief's
    own DOM, cookies, or storage. Anything outside the list is ``application/octet-stream``,
    which no browser renders as anything: the UI shows it as a file with a size and a
    download.
    """
    suffix = path.suffix.lower()
    if suffix in RENDERABLE:
        return RENDERABLE[suffix]
    # Only to make the "what is this" line in the UI useful; never used to decide rendering.
    guessed, _ = mimetypes.guess_type(path.name)
    return "application/octet-stream" if guessed is None else "application/octet-stream"


def resolve(ref: str, origin_dir: str | None) -> Path:
    """The file an artifact's ``ref`` names, or a refusal saying why it cannot be one.

    ``ref`` is an open string (REQ-46) and most of what it holds is not a local file at all,
    so the refusals here are ordinary rather than exceptional.
    """
    if not ref:
        raise ValidationFailed("this artifact has no reference to read")

    # A URL is somewhere else by definition. Two colons-and-slashes cases matter: http(s),
    # which the UI opens in a tab, and everything else, which is not ours to fetch.
    if "://" in ref or ref.startswith("mailto:"):
        raise ValidationFailed(
            "this artifact points at a URL rather than a file on this machine",
            details={"ref": ref},
        )

    path = Path(ref).expanduser()
    if not path.is_absolute():
        if not origin_dir:
            raise Conflict(
                "this artifact's path is relative and the workflow does not record where it "
                "ran, so there is nothing to resolve it against — set the folder on the "
                "workflow to read it",
                details={"ref": ref},
            )
        base = Path(origin_dir).expanduser()
        candidate = (base / path).resolve()
        # `../../..` in a ref would otherwise walk out of the directory the workflow ran in,
        # which is the one containment this endpoint has to enforce itself. An absolute ref
        # is a different case: the harness named that exact file deliberately.
        if not candidate.is_relative_to(base.resolve()):
            raise ValidationFailed(
                "this artifact's path climbs out of the folder the workflow ran in",
                details={"ref": ref},
            )
        return candidate
    return path.resolve()


def read(path: Path) -> tuple[bytes, str]:
    """The file's bytes, or a refusal. Never a directory, a device or a half-gigabyte."""
    try:
        stat = path.stat()
    except OSError:
        raise NotFound(f"no file at {path}", details={"path": str(path)}) from None

    if path.is_dir():
        raise ValidationFailed(
            "this artifact points at a directory rather than a file",
            details={"path": str(path)},
        )
    if not path.is_file():
        # A fifo or a device would block or stream forever; neither is an artifact.
        raise ValidationFailed(
            "this is not a regular file", details={"path": str(path)}
        )
    if stat.st_size > MAX_BYTES:
        raise ValidationFailed(
            f"file is {stat.st_size} bytes, over the {MAX_BYTES} byte preview limit",
            details={"path": str(path), "size": stat.st_size, "limit": MAX_BYTES},
        )
    try:
        return path.read_bytes(), media_type(path)
    except OSError as exc:
        raise NotFound(f"cannot read {path}: {exc}", details={"path": str(path)}) from None


#: Modules a component may be written in. No TypeScript: stripping types correctly is a
#: markedly harder parser than transforming JSX, and a component an agent writes beside its
#: own document has no need of them.
MODULE_SUFFIXES = (".jsx", ".js", ".mjs")

#: Enough for a document and the handful of components beside it. A cap rather than a
#: promise: the point is that a cycle or a sprawling tree fails loudly here instead of
#: hanging the browser.
MAX_MODULES = 24
MAX_MODULE_BYTES = 512 * 1024

_IMPORT = re.compile(
    r"""^\s*(?:import|export)\b[^'"\n]*?from\s*['"](?P<spec>[^'"]+)['"]"""
    r"""|^\s*import\s*['"](?P<bare>[^'"]+)['"]""",
    re.M,
)


def imports_of(source: str) -> list[str]:
    """Every module specifier a source names, in the order it names them."""
    out = []
    for match in _IMPORT.finditer(source):
        out.append(match.group("spec") or match.group("bare"))
    return out


def module_graph(entry: Path) -> dict[str, str]:
    """The entry file plus every co-located module it reaches, keyed by specifier.

    Resolution is deliberately tiny: only ``./name`` specifiers, only within the entry's own
    directory, only the suffixes above. A bare specifier like ``react`` is not resolved and
    not an error — the runtime supplies its own — and anything with a slash in it, or a
    ``..`` in it, is refused rather than searched for.

    That is what keeps CONTRACT-NOTES.md #34 intact through this. The client still names an
    artifact and nothing else; every path here is derived from the contents of files Chief
    already serves, and confined to the directory the named file sits in.
    """
    base = entry.parent.resolve()
    modules: dict[str, str] = {}
    queue = [(entry.name, entry)]
    seen = {entry.resolve()}

    while queue:
        key, path = queue.pop(0)
        if len(modules) >= MAX_MODULES:
            raise ValidationFailed(
                f"this document reaches more than {MAX_MODULES} modules",
                details={"limit": MAX_MODULES},
            )
        data, _ = read(path)
        if len(data) > MAX_MODULE_BYTES:
            raise ValidationFailed(
                f"{path.name} is over the {MAX_MODULE_BYTES} byte module limit",
                details={"path": str(path), "limit": MAX_MODULE_BYTES},
            )
        source = data.decode("utf-8", errors="replace")
        modules[key] = source

        for spec in imports_of(source):
            if not spec.startswith("./") or "/" in spec[2:] or ".." in spec:
                continue  # bare or nested: not ours to resolve
            stem = spec[2:]
            found = next(
                (
                    base / f"{stem}{suffix}"
                    for suffix in ("",) + MODULE_SUFFIXES
                    if (base / f"{stem}{suffix}").is_file()
                ),
                None,
            )
            if found is None:
                continue  # the runtime reports it; a missing sibling is not a server error
            if found.suffix.lower() not in MODULE_SUFFIXES:
                raise ValidationFailed(
                    f"{spec} is not a module this can serve",
                    details={"spec": spec, "suffix": found.suffix},
                )
            resolved = found.resolve()
            if not resolved.is_relative_to(base) or resolved in seen:
                continue
            seen.add(resolved)
            queue.append((spec, found))

    return modules
