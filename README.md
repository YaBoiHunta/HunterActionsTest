# HunterActionsTest

A prototype for keeping a repository's Markdown and its GitHub Wiki in sync
automatically, so documentation is readable without digging through the codebase.

## Documentation

Every Markdown file in this repository is published to the
**[wiki](https://github.com/YaBoiHunta/HunterActionsTest/wiki)** on each push to
`main`. Start at [Getting Started](docs/getting-started.md), or read the
[pipeline overview](docs/architecture/overview.md) for how it works.

Wiki pages are generated. Edit the Markdown here; anything typed directly into the
wiki UI is overwritten by the next sync.

## How it works

| Piece | Role |
| --- | --- |
| [`tools/wiki_sync.py`](tools/wiki_sync.py) | Flattens repo paths into wiki page names, rewrites relative links and images, and generates `Home`, `_Sidebar`, and `_Footer` |
| [`wiki.config.json`](wiki.config.json) | Which files to include or exclude, and the Home page title and intro |
| [`.github/workflows/wiki-sync.yml`](.github/workflows/wiki-sync.yml) | Builds and pushes the wiki on every push to `main` |
| [`.github/workflows/wiki-check.yml`](.github/workflows/wiki-check.yml) | Fails a pull request that would publish a broken link |

Preview the wiki locally, with no dependencies beyond Python 3:

```bash
python tools/wiki_sync.py --out .wiki-build   # build into .wiki-build/
python tools/wiki_sync.py --check             # validate links only
```
