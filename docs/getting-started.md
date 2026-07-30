---
title: Getting Started
wiki_order: 1
---

# Getting Started

Every Markdown file in this repository is published to the
[GitHub Wiki](https://github.com/YaBoiHunta/HunterActionsTest/wiki) automatically.
Write a document, push it to `main`, and a page appears. There is no separate
step to remember and no wiki page to keep in sync by hand.

## Adding a document

1. Create a Markdown file anywhere in the repository.
2. Link to other documents with ordinary relative paths.
3. Push to `main`.

The page name is derived from the file's full path, so `docs/architecture/overview.md`
becomes the page `Docs-Architecture-Overview`. Because the path is included, two files
named `notes.md` in different folders can never overwrite each other.

## Previewing before you push

The generator runs locally with no dependencies beyond Python 3:

```bash
python tools/wiki_sync.py --out .wiki-build
```

To validate links without writing anything, which is what pull request CI does:

```bash
python tools/wiki_sync.py --check
```

## Controlling a page

Optional YAML frontmatter overrides the defaults:

| Key | Effect |
| --- | --- |
| `wiki: false` | Skip the file entirely |
| `wiki_page` | Use an explicit page name instead of the flattened path |
| `wiki_title` | Change the link text used in the index and sidebar |
| `wiki_category` | Group the page under a different heading |
| `wiki_order` | Sort earlier within its group (lower comes first) |

## Next

Read the [architecture overview](architecture/overview.md) to see how the pieces
fit together, or browse the [architecture notes](architecture/) index.
