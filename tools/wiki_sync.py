#!/usr/bin/env python3
"""Build a GitHub Wiki tree from the Markdown files in this repository.

GitHub wikis are a flat namespace: a page committed to `sub/dir/Page.md` is still
served at `/wiki/Page`, so basenames must be unique repo-wide. Every source path
is therefore flattened into a single hyphenated page name, and relative links
between documents are rewritten to match. Hyphens render as spaces in wiki page
titles, which is why `-` is used as the path separator.

Usage:
    python tools/wiki_sync.py --out .wiki-build
    python tools/wiki_sync.py --check
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd")

DEFAULT_CONFIG = {
    "include": ["**/*.md"],
    "exclude": ["README.md", ".github/**", "node_modules/**", ".wiki-build/**", ".git/**"],
    "home_title": "Knowledge Base",
    "home_intro": "",
    "recent_count": 10,
    "backlinks": True,
}

# Characters that are illegal in Windows filenames or that break wiki page URLs.
UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

INLINE_LINK_RE = re.compile(
    r"""(?P<bang>!?)\[(?P<text>(?:[^\[\]\\]|\\.)*)\]\(\s*"""
    r"""(?P<target><[^>]*>|[^()\s]+)"""
    r"""(?P<title>\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?\s*\)""",
    re.VERBOSE,
)
REF_DEF_RE = re.compile(
    r"""^(?P<lead>\s{0,3}\[(?:[^\[\]\\]|\\.)+\]:\s*)"""
    r"""(?P<target><[^>]*>|\S+)"""
    r"""(?P<rest>.*)$""",
    re.VERBOSE,
)
HTML_ATTR_RE = re.compile(r"""(?P<lead>\b(?:src|href)\s*=\s*)(?P<q>["'])(?P<target>[^"']*)(?P=q)""")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


class WikiSyncError(Exception):
    """Fatal configuration or content problem that must stop the build."""


@dataclass
class Page:
    source: str
    name: str
    title: str
    category: str
    order: int
    body: str
    meta: dict
    updated: str | None = None
    rendered_body: str = ""
    rendered: str = ""


@dataclass
class Report:
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def warn(self, source: str, message: str) -> None:
        self.warnings.append((source, message))


# --------------------------------------------------------------------------- #
# Globbing
# --------------------------------------------------------------------------- #


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-flavoured glob into a full-match regex.

    `**/` spans zero or more directories, `*` and `?` never cross a `/`.
    """
    parts: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def matches_any(path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.match(path) for p in patterns)


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading `---` YAML block off the document.

    Only the flat `key: value` subset is understood, which keeps this script
    dependency-free. Nested structures are ignored rather than erroring.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return _parse_flat_yaml(lines[1:idx]), "\n".join(lines[idx + 1 :])
    return {}, text


def _parse_flat_yaml(lines: list[str]) -> dict:
    meta: dict = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t", "-"):
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = _coerce_scalar(raw.strip())
    return meta


def _coerce_scalar(raw: str):
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    lowered = raw.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


# --------------------------------------------------------------------------- #
# Page naming
# --------------------------------------------------------------------------- #


def titleize(segment: str) -> str:
    """Turn one path segment into hyphen-joined title-cased words.

    Only the first letter is touched so existing casing such as `API` survives.
    """
    words = [w for w in re.split(r"[-_\s]+", segment) if w]
    return "-".join(w[:1].upper() + w[1:] for w in words)


def sanitize_page_name(name: str) -> str:
    cleaned = UNSAFE_NAME_CHARS.sub("-", name).strip(" .-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned or "Page"


def page_name_for(rel_path: str) -> str:
    """Flatten a repo-relative Markdown path into a unique wiki page name.

    A directory's `README.md` becomes that directory's index page, so
    `docs/architecture/README.md` and `docs/architecture/overview.md` yield
    `Docs-Architecture` and `Docs-Architecture-Overview`.
    """
    pure = PurePosixPath(rel_path)
    parts = list(pure.parts)
    if pure.stem.lower() == "readme" and len(parts) > 1:
        segments = parts[:-1]
    else:
        segments = parts[:-1] + [pure.stem]
    return sanitize_page_name("-".join(titleize(s) for s in segments))


def display_title(page_name: str) -> str:
    """The title GitHub will show, which is the filename with hyphens as spaces."""
    return page_name.replace("-", " ")


# --------------------------------------------------------------------------- #
# Git metadata
# --------------------------------------------------------------------------- #


def run_git(args: list[str], root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def git_last_modified(root: Path) -> dict[str, str]:
    """Map repo-relative path -> ISO date of the most recent commit touching it.

    One `git log` walk rather than a subprocess per file.
    """
    out = run_git(["log", "--pretty=format:\x01%cI", "--name-only"], root)
    if out is None:
        return {}
    dates: dict[str, str] = {}
    current: str | None = None
    for line in out.split("\n"):
        if line.startswith("\x01"):
            current = line[1:].strip()
        elif line.strip() and current and line.strip() not in dates:
            dates[line.strip()] = current
    return dates


def resolve_repo(root: Path, explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env.strip()
    url = run_git(["remote", "get-url", "origin"], root)
    if url:
        match = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/\s]+?)(?:\.git)?\s*$", url.strip())
        if match:
            return match.group("slug")
    raise WikiSyncError(
        "Could not determine the repository slug. Pass --repo OWNER/NAME or set GITHUB_REPOSITORY."
    )


# --------------------------------------------------------------------------- #
# Link rewriting
# --------------------------------------------------------------------------- #


class LinkRewriter:
    def __init__(
        self,
        pages_by_source: dict[str, Page],
        tracked_files: set[str],
        repo: str,
        branch: str,
        report: Report,
    ) -> None:
        self.pages_by_source = pages_by_source
        self.tracked_files = tracked_files
        self.repo = repo
        self.branch = branch
        self.report = report
        self.blob_base = f"https://github.com/{repo}/blob/{branch}"
        self.raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}"
        # source path -> wiki page names it links to, collected while rewriting
        # so backlinks cost nothing extra to compute.
        self.edges: dict[str, set[str]] = {}

    def rewrite_document(self, source: str, text: str) -> str:
        self.edges.setdefault(source, set())
        lines_out: list[str] = []
        fence: tuple[str, int] | None = None
        for line in text.split("\n"):
            match = FENCE_RE.match(line.lstrip())
            if match:
                token = match.group(1)
                if fence is None:
                    fence = (token[0], len(token))
                elif token[0] == fence[0] and len(token) >= fence[1]:
                    fence = None
                lines_out.append(line)
                continue
            if fence is not None:
                lines_out.append(line)
                continue
            lines_out.append(self._rewrite_line(source, line))
        return "\n".join(lines_out)

    def _rewrite_line(self, source: str, line: str) -> str:
        stash: list[str] = []

        def hide(match: re.Match[str]) -> str:
            stash.append(match.group(0))
            return f"\x00{len(stash) - 1}\x00"

        masked = INLINE_CODE_RE.sub(hide, line)

        def inline(match: re.Match[str]) -> str:
            target = match.group("target")
            new = self._map_target(source, target, is_image=bool(match.group("bang")))
            if new == target:
                return match.group(0)
            return (
                f"{match.group('bang')}[{match.group('text')}]"
                f"({new}{match.group('title') or ''})"
            )

        def refdef(match: re.Match[str]) -> str:
            target = match.group("target")
            new = self._map_target(source, target, is_image=False)
            return f"{match.group('lead')}{new}{match.group('rest')}"

        def html_attr(match: re.Match[str]) -> str:
            target = match.group("target")
            new = self._map_target(source, target, is_image=match.group("lead").strip().startswith("src"))
            return f"{match.group('lead')}{match.group('q')}{new}{match.group('q')}"

        masked = INLINE_LINK_RE.sub(inline, masked)
        masked = REF_DEF_RE.sub(refdef, masked)
        masked = HTML_ATTR_RE.sub(html_attr, masked)
        return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], masked)

    def _map_target(self, source: str, target: str, is_image: bool) -> str:
        raw = target.strip()
        wrapped = raw.startswith("<") and raw.endswith(">")
        if wrapped:
            raw = raw[1:-1]
        mapped = self._map_path(source, raw, is_image)
        return f"<{mapped}>" if wrapped else mapped

    def _map_path(self, source: str, target: str, is_image: bool) -> str:
        if not target or target.startswith("#") or target.startswith("//"):
            return target
        if SCHEME_RE.match(target):
            return target

        path_part, hash_sep, anchor = target.partition("#")
        path_part, query_sep, query = path_part.partition("?")
        if not path_part:
            return target

        suffix = f"{query_sep}{query}{hash_sep}{anchor}"
        decoded = path_part.replace("%20", " ")

        if decoded.startswith("/"):
            resolved = decoded.lstrip("/")
        else:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded))
        if resolved in (".", ""):
            resolved = ""
        if resolved.startswith(".."):
            self.report.warn(source, f"link escapes the repository root: {target}")
            return target

        page = self._lookup_page(resolved)
        if page is not None:
            self.edges.setdefault(source, set()).add(page.name)
            return f"{page.name}{suffix}"

        if resolved in self.tracked_files:
            base = self.raw_base if is_image else self.blob_base
            return f"{base}/{_url_quote(resolved)}{suffix}"

        self.report.warn(source, f"unresolved link target: {target}")
        return target

    def _lookup_page(self, resolved: str) -> Page | None:
        if resolved in self.pages_by_source:
            return self.pages_by_source[resolved]
        # A link to a directory means that directory's README index page.
        for suffix in MARKDOWN_SUFFIXES:
            candidate = posixpath.join(resolved, f"README{suffix}") if resolved else f"README{suffix}"
            if candidate in self.pages_by_source:
                return self.pages_by_source[candidate]
        # A link that omits the extension, e.g. `[x](./overview)`.
        for suffix in MARKDOWN_SUFFIXES:
            if resolved + suffix in self.pages_by_source:
                return self.pages_by_source[resolved + suffix]
        return None


def _url_quote(path: str) -> str:
    from urllib.parse import quote

    return quote(path, safe="/._-~")


# --------------------------------------------------------------------------- #
# Discovery and building
# --------------------------------------------------------------------------- #


def load_config(root: Path, config_path: Path | None) -> dict:
    path = config_path or (root / "wiki.config.json")
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WikiSyncError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise WikiSyncError(f"{path} must contain a JSON object.")
        config.update(loaded)
    return config


def collect_files(root: Path, config: dict) -> list[str]:
    includes = [glob_to_regex(p) for p in config["include"]]
    excludes = [glob_to_regex(p) for p in config["exclude"]]
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for filename in sorted(filenames):
            abs_path = Path(dirpath) / filename
            rel = abs_path.relative_to(root).as_posix()
            if not matches_any(rel, includes) or matches_any(rel, excludes):
                continue
            found.append(rel)
    return found


def all_tracked_files(root: Path) -> set[str]:
    """Every file in the repo, used to turn asset links into absolute GitHub URLs."""
    files: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for filename in filenames:
            files.add((Path(dirpath) / filename).relative_to(root).as_posix())
    return files


def build_pages(root: Path, sources: list[str], report: Report) -> list[Page]:
    pages: list[Page] = []
    claimed: dict[str, str] = {}
    for rel in sources:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        if meta.get("wiki") is False:
            continue

        override = meta.get("wiki_page")
        name = sanitize_page_name(str(override)) if override else page_name_for(rel)
        if name in claimed:
            raise WikiSyncError(
                f"Wiki page name collision: '{name}' is produced by both "
                f"'{claimed[name]}' and '{rel}'. Rename one file or set a "
                f"'wiki_page' value in its frontmatter."
            )
        claimed[name] = rel

        parts = PurePosixPath(rel).parts
        default_category = titleize(parts[0]).replace("-", " ") if len(parts) > 1 else "Root"
        category = str(meta.get("wiki_category") or default_category)
        order = meta.get("wiki_order")
        title = str(meta.get("wiki_title") or meta.get("title") or display_title(name))

        pages.append(
            Page(
                source=rel,
                name=name,
                title=title,
                category=category,
                order=int(order) if isinstance(order, int) else 100,
                body=body,
                meta=meta,
            )
        )
    return pages


def compute_backlinks(pages: list[Page], edges: dict[str, set[str]]) -> dict[str, set[str]]:
    """Invert the link graph: page name -> names of pages that link to it.

    Only links written in source documents count. Home and _Sidebar link to
    everything, so counting generated navigation would mean nothing is ever an
    orphan.
    """
    by_source = {page.source: page for page in pages}
    backlinks: dict[str, set[str]] = {}
    for source, targets in edges.items():
        origin = by_source.get(source)
        if origin is None:
            continue
        for target in targets:
            if target != origin.name:
                backlinks.setdefault(target, set()).add(origin.name)
    return backlinks


def render_pages(
    pages: list[Page],
    root: Path,
    repo: str,
    branch: str,
    report: Report,
    show_backlinks: bool = True,
) -> dict[str, set[str]]:
    by_source = {page.source: page for page in pages}
    rewriter = LinkRewriter(by_source, all_tracked_files(root), repo, branch, report)
    dates = git_last_modified(root)

    # Every document must be rewritten before any page is finalised, because a
    # page's backlinks depend on documents rendered after it.
    for page in pages:
        page.updated = dates.get(page.source) or _mtime_iso(root / page.source)
        page.rendered_body = rewriter.rewrite_document(page.source, page.body).strip("\n")

    backlinks = compute_backlinks(pages, rewriter.edges) if show_backlinks else {}
    by_name = {page.name: page for page in pages}

    for page in pages:
        banner = (
            f"> Auto-generated from "
            f"[`{page.source}`](https://github.com/{repo}/blob/{branch}/{_url_quote(page.source)})"
            f"{_updated_suffix(page.updated)}. Edits made here will be overwritten."
        )
        sections = [banner, "", page.rendered_body]
        referrers = sorted(backlinks.get(page.name, ()), key=lambda n: by_name[n].title.lower())
        if referrers:
            links = ", ".join(f"[{by_name[name].title}]({name})" for name in referrers)
            sections += ["", "---", "", f"**Referenced by:** {links}"]
        page.rendered = "\n".join(sections) + "\n"

    return backlinks


def _updated_suffix(updated: str | None) -> str:
    day = _as_day(updated)
    return f" &middot; last updated {day}" if day else ""


def _as_day(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10]


def _mtime_iso(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Navigation pages
# --------------------------------------------------------------------------- #


def group_pages(pages: list[Page]) -> list[tuple[str, list[Page]]]:
    groups: dict[str, list[Page]] = {}
    for page in pages:
        groups.setdefault(page.category, []).append(page)
    for items in groups.values():
        items.sort(key=lambda p: (p.order, p.title.lower()))
    # "Root" last so top-level stray docs do not lead the index.
    return sorted(groups.items(), key=lambda kv: (kv[0] == "Root", kv[0].lower()))


def render_home(
    pages: list[Page],
    config: dict,
    repo: str,
    branch: str,
    backlinks: dict[str, set[str]] | None = None,
) -> str:
    lines = [f"# {config['home_title']}", ""]
    intro = str(config.get("home_intro") or "").strip()
    if intro:
        lines += [intro, ""]

    lines += [
        f"[Browse the repository](https://github.com/{repo}) &middot; "
        f"[README](https://github.com/{repo}/blob/{branch}/README.md)",
        "",
    ]

    if not pages:
        lines += ["No documentation pages yet. Add a Markdown file to the repository.", ""]
        return "\n".join(lines)

    recent_count = int(config.get("recent_count") or 0)
    dated = [p for p in pages if p.updated]
    if recent_count > 0 and dated:
        dated.sort(key=lambda p: p.updated or "", reverse=True)
        lines += ["## Recently updated", ""]
        for page in dated[:recent_count]:
            lines.append(f"- [{page.title}]({page.name}) &middot; {_as_day(page.updated)}")
        lines.append("")

    lines += ["## All pages", ""]
    for category, items in group_pages(pages):
        lines += [f"### {category}", ""]
        for page in items:
            lines.append(f"- [{page.title}]({page.name}) &mdash; `{page.source}`")
        lines.append("")

    if backlinks is not None:
        orphans = [p for p in pages if not backlinks.get(p.name)]
        if orphans and len(orphans) != len(pages):
            lines += [
                "## Not linked from anywhere",
                "",
                "These pages are only reachable from this index. Linking to them "
                "from a related document makes them easier to find.",
                "",
            ]
            for page in sorted(orphans, key=lambda p: p.title.lower()):
                lines.append(f"- [{page.title}]({page.name}) &mdash; `{page.source}`")
            lines.append("")

    total = len(pages)
    lines.append(f"_{total} page{'s' if total != 1 else ''} generated from Markdown in the repository._")
    return "\n".join(lines) + "\n"


def render_sidebar(pages: list[Page], config: dict) -> str:
    lines = [f"### {config['home_title']}", "", "- [Home](Home)"]
    for category, items in group_pages(pages):
        lines.append(f"- **{category}**")
        for page in items:
            lines.append(f"  - [{page.title}]({page.name})")
    return "\n".join(lines) + "\n"


def render_footer(repo: str, branch: str, sha: str | None) -> str:
    commit = f"[`{sha[:7]}`](https://github.com/{repo}/commit/{sha})" if sha else branch
    return (
        f"Generated from [{repo}](https://github.com/{repo}) at {commit}. "
        f"These pages are produced by `tools/wiki_sync.py`; "
        f"edit the Markdown in the repository, not the wiki.\n"
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def write_output(out_dir: Path, root: Path, files: dict[str, str]) -> None:
    resolved_out = out_dir.resolve()
    if resolved_out == root.resolve() or resolved_out.parent == resolved_out:
        raise WikiSyncError(f"Refusing to write the wiki build into '{resolved_out}'.")
    if resolved_out.exists():
        shutil.rmtree(resolved_out)
    resolved_out.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = resolved_out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)


def emit_warnings(report: Report) -> None:
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for source, message in report.warnings:
        if in_actions:
            print(f"::warning file={source}::{message}")
        else:
            print(f"warning: {source}: {message}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub Wiki pages from repository Markdown.")
    parser.add_argument("--root", default=".", help="Repository root (default: current directory).")
    parser.add_argument("--out", default=".wiki-build", help="Directory to write wiki pages into.")
    parser.add_argument("--config", default=None, help="Path to wiki.config.json.")
    parser.add_argument("--repo", default=None, help="OWNER/NAME slug used to build absolute URLs.")
    parser.add_argument("--branch", default=None, help="Branch used in generated URLs (default: main).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate only: write nothing and exit non-zero if any link is unresolved.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: repository root '{root}' does not exist.", file=sys.stderr)
        return 2

    try:
        config = load_config(root, Path(args.config).resolve() if args.config else None)
        repo = resolve_repo(root, args.repo)
        branch = args.branch or os.environ.get("GITHUB_REF_NAME") or "main"
        report = Report()

        sources = collect_files(root, config)
        pages = build_pages(root, sources, report)
        show_backlinks = config.get("backlinks", True) is not False
        backlinks = render_pages(pages, root, repo, branch, report, show_backlinks)
    except WikiSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    files = {f"{page.name}.md": page.rendered for page in pages}
    files["Home.md"] = render_home(pages, config, repo, branch, backlinks if show_backlinks else None)
    files["_Sidebar.md"] = render_sidebar(pages, config)
    files["_Footer.md"] = render_footer(repo, branch, (run_git(["rev-parse", "HEAD"], root) or "").strip() or None)

    emit_warnings(report)

    if args.check:
        print(f"Checked {len(pages)} page(s) with {len(report.warnings)} warning(s).")
        if report.warnings:
            print("error: unresolved links found; fix them or make the targets absolute.", file=sys.stderr)
            return 1
        return 0

    try:
        write_output(Path(args.out), root, files)
    except WikiSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(files)} file(s) for {len(pages)} page(s) to {Path(args.out).resolve()}")
    for page in pages:
        print(f"  {page.source} -> {page.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
