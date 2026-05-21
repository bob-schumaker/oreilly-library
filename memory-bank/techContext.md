# Tech Context

## Language and Packaging

- Python 3.14+.
- Poetry is the configured packaging and dependency-management tool.
- CLI script entry point: `oreilly-library = "oreilly_library.__main__:main"`.

## Runtime Dependencies

- `requests` for HTTP sessions.
- `cobblerslib[docopt]` for docopt-style CLI parsing and shared utilities.
- `selenium` for interactive cookie capture through Chrome.
- `tqdm` for progress display when processing multiple identifiers.
- `ebooklib` for EPUB construction.
- `appscript` for macOS browser-tab access when `--browser` is used.
- `sqlite3` from the Python standard library for early-release tracking in
  `~/.cache/oreilly-early-release.db`.

## Development and Tooling

- `poetry install` installs the project.
- `.pre-commit-config.yaml` configures YAML/JSON hooks plus local `tomlcheck`,
  `black`, `isort`, and `flake8` hooks.
- Development dependency group includes `pre-commit`.
- The active pre-commit environment also ran `ruff` and `ruff format` for Python
  files during the `.htm` builder fix commit.

## External Tools

- Google Chrome and a compatible Chrome WebDriver are needed for Selenium login.
- `epubcheck` or `epubchecker` is required when using `--epubcheck`.
- Calibre's `ebook-polish` is used when `--clean` is requested and the tool is
  discoverable.

## Environment Variables

- `SAFARICOOKIES_PATH` — default cookie file path.
- `SAFARIBOOKS_PATH` — base working directory when `--output-dir` is not set.
- `OREILLY_LOGIN_URL` — login URL override for Selenium capture.

## Repository State at Initialization

- Memory bank initialization observed existing unrelated local changes and
  untracked files. Future commit preparation should stage only task-related
  memory-bank files unless the user explicitly expands scope.
- After the `.htm` builder fix, unrelated local changes still included
  `pyproject.toml` plus several untracked repository files/directories; keep
  future commits narrowly staged unless the user expands scope.
