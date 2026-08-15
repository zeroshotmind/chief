# Pushing this to GitHub

The working tree in this folder **is** the project — flattened to the top level and renamed
from Orchestrator to Chief. It is not a git repository yet.

`orchestrator.bundle` is a separate archive: 13 commits authored
`Swastik Roy <swastikroy1993@gmail.com>`, no assistant attribution, captured **before** the
rename. Its tree still uses the old `orchestrator/` layout and package name, so it is
history, not the current source. Keep it if you want the provenance; the files here are
ahead of it.

## Starting a repo from the current tree

Create an empty private repo (no README, no .gitignore, no licence), then:

```bash
git init
git add .
git commit -m "Chief: initial commit"
git remote add origin git@github.com:zeroshotmind/chief.git
git push -u origin main
```

## If you'd rather keep the 13 commits

Restore the bundle first, then replay the rename on top of it as its own commit:

```bash
git clone orchestrator.bundle chief-history
# copy the current tree over it, commit the rename, then push
```

That preserves the history but costs you a slightly awkward first diff, since the rename
touches nearly every file.

## Verifying before you push

```bash
pip install -e ".[dev]"
pytest                      # 177 tests
ruff check .
node scripts/smoke_ui.mjs   # renders every UI screen headlessly; needs node
NO_TEMPLATES=1 node scripts/smoke_ui.mjs   # same, against a server too old to have /templates
```

The smoke script is deliberately outside pytest — it needs node, which the package does not
otherwise require — so it will not run itself. It is the only automated coverage the web UI
has.
