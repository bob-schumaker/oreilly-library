"""
An application to download books from the O'Reilly Safari library for local
consumption. You will need a valid login for O'Reilly in order to do this.

Usage:
    oreilly-library [--verbose] [--debug] [--epubcheck] [--clean] [--build] [--output-dir=OUTPUT] [--cookie-file=FILE] [--login=BROWSER] [--browser | ISBN...]
    oreilly-library --check [--fetch] [--verbose] [--debug] [--output-dir=OUTPUT] [--cookie-file=FILE] [--login=BROWSER]

Arguments:
    ISBN            Look for these books

Options:
    --browser           Get the open URLs from the browser and load the ones that are Safari books
    --build             Build EPUBs from previously downloaded local files without downloading again
    --output-dir=OUTPUT Put the output files here. [Default: working/Books]
    --cookie-file=FILE  Use cookies from FILE. If absent, start a Selenium login flow.
    --login=BROWSER     Force Selenium login to refresh cookies using BROWSER.
    --epubcheck         Run epubcheck validation after building each EPUB.
    --check             Check tracked early-release books for updated metadata.
    --fetch             Fetch updated books found by --check and update tracking metadata.
    --clean             Run Calibre ebook-polish cleanup after building each EPUB.
    --verbose           Make some noise
    --debug             Make a lot of noise
"""

import json
import os
import re
import subprocess
import sys
import time
from logging import getLogger
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import requests
from tqdm import tqdm

from oreilly_library.cobblerslib import docopt, docopt_arguments, general, text_input

if sys.platform == "darwin":
    from appscript import app

from oreilly_library.epub_builder import EpubBuilder
from oreilly_library.epub_downloader import EpubDownloader
from oreilly_library.early_release_tracker import (
    EarlyReleaseTracker,
    TrackedBook,
    find_updated_books,
    metadata_is_roughcut,
    tracked_book_from_metadata,
)

PATH = os.environ.get("SAFARIBOOKS_PATH") or "working"
DEFAULT_CHROME_VERSION = "147.0.7727.138"


class oreilly_loader:
    """Download an epub from O'Reilly Safari."""

    def __init__(
        self,
        isbns: Optional[Sequence[str]] = None,
        output_dir: Optional[str] = None,
        check: bool | None = None,
        epubcheck: bool | None = None,
        fetch: bool | None = None,
        clean: bool | None = None,
        build: bool | None = None,
        verbose: bool | None = None,
        debug: bool | None = None,
        cookie_file: Optional[str] = None,
        browser: str | None = None,
        login: str | bool | None = None,
        **kwargs,
    ) -> None:
        self._check_updates = bool(check)
        self._fetch_updates = bool(fetch)
        if self._fetch_updates and not self._check_updates:
            raise ValueError("--fetch requires --check.")
        if self._check_updates and build:
            raise ValueError("--check cannot be combined with --build.")

        if isinstance(isbns, str):
            isbns = [isbns]
        elif isinstance(isbns, Iterable):
            isbns = list(isbns)
        if not isbns:
            if not browser and not self._check_updates:
                raise ValueError("No ISBN identifiers or --browser flag provided.")
            self._identifiers = []
        else:
            self._identifiers = list(isbns)
        self._output_dir = output_dir
        self._epubcheck = bool(epubcheck)
        self._clean = bool(clean)
        self._build = bool(build)
        self._browser = browser
        self._verbose = bool(verbose)
        self._debug = bool(debug)
        self.logger = getLogger(self.__class__.__name__)
        self._tracker = EarlyReleaseTracker()
        cookies_env = os.environ.get("SAFARICOOKIES_PATH")
        self._cookies_file = Path(
            cookie_file or cookies_env or os.path.join(PATH, "cookies.json")
        )
        self._login = self._resolve_login_configuration(
            login=login,
        )
        self.session: requests.Session | None = None
        if not self._build and not self._check_updates:
            self._initialize_session()

    def download_epub(self, identifier: str, output_dir: Optional[str] = None) -> None:
        """Download and build an EPUB, or rebuild it from local files."""
        target_dir: Path
        if output_dir is not None:
            target_dir = Path(output_dir)
        elif self._output_dir is not None:
            target_dir = Path(self._output_dir)
        else:
            target_dir = Path(PATH) / "Books"

        if self._build:
            source_dir = self._resolve_existing_source_dir(identifier, target_dir)
            if self._verbose or self._debug:
                self.logger.info(
                    "Building EPUB for %s from local files in %s",
                    identifier,
                    source_dir,
                )
            builder = EpubBuilder(
                source_dir,
                check=self._epubcheck,
                verbose=self._verbose and not self._debug,
                debug=self._debug,
            )
            builder.build_epub(clean=self._clean)
            self._record_local_early_release(source_dir, identifier)
            return

        if self.session is None:
            raise RuntimeError("Authenticated session was not initialized.")

        downloader = EpubDownloader(
            identifier=identifier,
            session=self.session,
            output_dir=target_dir,
            check=self._epubcheck,
            clean=self._clean,
            verbose=self._verbose and not self._debug,
            debug=self._debug,
        )
        downloader.download_and_build()

        if downloader.book_info:
            self._sync_early_release_metadata(downloader.book_info, identifier)

    def _resolve_existing_source_dir(self, identifier: str, base_dir: Path) -> Path:
        candidates: list[Path] = []

        identifier_path = Path(identifier).expanduser()
        if identifier_path.is_dir():
            candidates.append(identifier_path)

        candidates.extend([base_dir / identifier, base_dir])

        if base_dir.exists():
            child_dirs = sorted(path for path in base_dir.iterdir() if path.is_dir())
            for child in child_dirs:
                if child.name == identifier or child.name.endswith(f"({identifier})"):
                    candidates.append(child)
            candidates.extend(child_dirs)

        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if self._directory_matches_identifier(resolved, identifier):
                return resolved

        raise FileNotFoundError(
            "Unable to find previously downloaded files for "
            f"{identifier!r} under {base_dir}."
        )

    def _directory_matches_identifier(self, directory: Path, identifier: str) -> bool:
        if not directory.is_dir():
            return False

        if (directory / f"{identifier}.json").exists():
            return True

        if directory.name == identifier or directory.name.endswith(f"({identifier})"):
            return True

        for metadata_path in sorted(directory.glob("*.json")):
            try:
                with metadata_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except OSError:
                continue
            except json.JSONDecodeError:
                continue

            if not isinstance(payload, dict):
                continue

            known_identifiers = {
                str(value)
                for value in (
                    payload.get("identifier"),
                    payload.get("isbn"),
                    payload.get("id"),
                )
                if value is not None
            }
            if identifier in known_identifiers:
                return True

        return False

    def _record_local_early_release(self, source_dir: Path, identifier: str) -> None:
        metadata = self._load_book_metadata(source_dir, identifier)
        if metadata is not None:
            self._sync_early_release_metadata(metadata, identifier)

    def _sync_early_release_metadata(
        self,
        metadata: Mapping[str, Any],
        identifier: str,
    ) -> None:
        if metadata_is_roughcut(metadata):
            self._record_early_release_metadata(metadata, identifier)
            return
        self._tracker.delete(identifier)

    def _record_early_release_metadata(
        self,
        metadata: Mapping[str, Any],
        identifier: str,
    ) -> None:
        tracked_book = tracked_book_from_metadata(
            metadata,
            fallback_identifier=identifier,
        )
        if tracked_book is None:
            self.logger.warning(
                "Unable to track early-release metadata for %s; required fields missing.",
                identifier,
            )
            return
        self._tracker.upsert(tracked_book)

    def _load_book_metadata(
        self,
        source_dir: Path,
        identifier: str,
    ) -> Mapping[str, Any] | None:
        candidate_paths = [source_dir / f"{identifier}.json"]
        candidate_paths.extend(sorted(source_dir.glob("*.json")))
        seen: set[Path] = set()
        for metadata_path in candidate_paths:
            if metadata_path in seen:
                continue
            seen.add(metadata_path)
            try:
                with metadata_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except OSError:
                continue
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping) and self._metadata_matches_identifier(
                payload,
                identifier,
            ):
                return payload
        return None

    def _metadata_matches_identifier(
        self,
        metadata: Mapping[str, Any],
        identifier: str,
    ) -> bool:
        known_identifiers = {
            str(value)
            for value in (
                metadata.get("identifier"),
                metadata.get("isbn"),
                metadata.get("id"),
            )
            if value is not None
        }
        return identifier in known_identifiers

    @staticmethod
    def _detect_chrome_version() -> str:
        chrome_bin = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        try:
            result = subprocess.run(
                [chrome_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except OSError:
            return DEFAULT_CHROME_VERSION
        except subprocess.SubprocessError:
            return DEFAULT_CHROME_VERSION

        if result.returncode != 0:
            return DEFAULT_CHROME_VERSION

        parts = result.stdout.strip().split()
        if not parts:
            return DEFAULT_CHROME_VERSION
        return parts[-1]

    @classmethod
    def _build_chrome_user_agent(cls) -> str:
        chrome_version = cls._detect_chrome_version()
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36"
        )

    @staticmethod
    def _normalize_browser_name(
        browser: str | None,
        *,
        fallback: str | None = "chrome",
    ) -> str:
        normalized_browser = (browser or "").strip().lower()
        if normalized_browser:
            return normalized_browser
        if fallback is not None:
            return fallback
        raise ValueError("A browser name is required when using --login=BROWSER.")

    def _resolve_login_configuration(
        self,
        login: str | bool | None,
    ) -> tuple[str, bool]:
        if isinstance(login, str):
            return self._normalize_browser_name(login, fallback=None), True
        return "chrome", bool(login)

    def _initialize_session(self) -> None:
        if self.session is not None:
            return
        self.session = requests.Session()
        cookie_data = self._ensure_cookies()
        self._apply_cookie_data(cookie_data)
        chrome_user_agent = self._build_chrome_user_agent()
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://learning.oreilly.com/",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": chrome_user_agent,
        }
        self.session.headers.update(headers)

    def run(self) -> int:
        if self._output_dir and not Path(self._output_dir).exists():
            Path(self._output_dir).mkdir(parents=True, exist_ok=True)
        if self._check_updates:
            return self._run_early_release_check()
        if self._browser:
            for tab in app("Google Chrome").windows[0].tabs():
                url = tab.URL()
                if not url or "learning.oreilly.com" not in url:
                    continue
                identifier = Path(url).name
                if identifier:
                    identifier = str(identifier)
                    if re.match(r"^[0-9]+$", identifier):
                        self._identifiers.append(identifier)
                        if self._debug:
                            self.logger.debug(f"Found identifier {identifier}")
        if len(self._identifiers) > 1 and not self._verbose and not self._debug:
            iterator = tqdm(self._identifiers)
        else:
            iterator = self._identifiers
        for identifier in iterator:
            self.download_epub(identifier, output_dir=self._output_dir)
        return 0

    def _run_early_release_check(self) -> int:
        tracked_books = self._tracker.list_books()
        if not tracked_books:
            print("No early-release books are currently tracked.")
            return 0

        self._initialize_session()

        remote_books: dict[str, TrackedBook] = {}
        result_messages: list[str] = []
        iterator = tracked_books
        if len(tracked_books) > 1 and not self._verbose and not self._debug:
            iterator = tqdm(tracked_books)
        for tracked in iterator:
            try:
                metadata = self._fetch_remote_book_metadata(tracked.book_id)
            except requests.HTTPError as exc:
                response = exc.response
                if response is not None and response.status_code == 404:
                    self._tracker.delete(tracked.book_id)
                    continue
                raise
            if not metadata_is_roughcut(metadata):
                self._tracker.delete(tracked.book_id)
                continue
            remote = tracked_book_from_metadata(
                metadata,
                fallback_identifier=tracked.book_id,
            )
            if remote is not None:
                remote_books[tracked.book_id] = remote

        updated_books = find_updated_books(tracked_books, remote_books)
        result_messages.extend(
            f"{updated.remote.book_title} ({updated.tracked.book_id}): "
            f"{updated.tracked.last_modified_time} -> "
            f"{updated.remote.last_modified_time}"
            for updated in updated_books
        )

        if result_messages:
            for message in result_messages:
                print(message)
        else:
            print("No tracked early-release books have updates.")

        if not updated_books:
            return 0

        if self._fetch_updates:
            for updated in updated_books:
                self.download_epub(updated.tracked.book_id, output_dir=self._output_dir)
                self._tracker.upsert(updated.remote)

        return 0

    def _fetch_remote_book_metadata(self, identifier: str) -> Mapping[str, Any]:
        if self.session is None:
            raise RuntimeError("Authenticated session was not initialized.")
        downloader = EpubDownloader(
            identifier=identifier,
            session=self.session,
            output_dir=Path(self._output_dir)
            if self._output_dir
            else Path(PATH) / "Books",
            check=False,
            clean=False,
            verbose=self._verbose and not self._debug,
            debug=self._debug,
        )
        return downloader.fetch_bookinfo()

    def _ensure_cookies(self) -> list[dict] | dict:
        if self._login[1]:
            self.logger.info(
                f"--login={self._login} supplied. Launching Selenium to refresh cookies.",
            )
            cookie_data = self._collect_cookies_with_selenium(self._cookies_file)
            self._persist_cookies(cookie_data, self._cookies_file)
            return cookie_data

        if self._cookies_file.exists():
            return self._load_cookies_from_file(self._cookies_file)
        cookie_data = self._collect_cookies_with_selenium(self._cookies_file)
        self._persist_cookies(cookie_data, self._cookies_file)
        return cookie_data

    def _load_cookies_from_file(self, path: Path) -> list[dict] | dict:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _persist_cookies(self, cookie_data: list[dict] | dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(cookie_data, fh, indent=2)

    def _apply_cookie_data(self, cookie_data: list[dict] | dict) -> None:
        # If the cookies are in a list, assume that each entry is a dict
        # that has, at least, name and value keys. For example, the
        # output of "EatThisCookie" in Chrome.
        if isinstance(cookie_data, list):
            for cookie in cookie_data:
                name = cookie.get("name")
                value = cookie.get("value")
                if name is None or value is None:
                    continue
                domain = cookie.get("domain")
                path = cookie.get("path")
                expiry = cookie.get("expiry")
                self.session.cookies.set(
                    name,
                    value,
                    domain=domain,
                    path=path,
                    expires=expiry,
                )
        else:
            self.session.cookies.update(cookie_data)

    def _collect_cookies_with_selenium(self, path: Path) -> list[dict]:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import WebDriverException
            from selenium.webdriver.chrome.options import Options as ChromeOptions
        except ModuleNotFoundError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError(
                "Selenium is required to collect cookies automatically. "
                "Install it with 'pip install selenium' or provide a cookie file."
            ) from exc

        browser_name = self._login[0]
        if browser_name != "chrome":
            raise RuntimeError(
                f"Unsupported browser '{browser_name}'. Only 'chrome' is currently supported."
            )

        chrome_options = ChromeOptions()
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--no-sandbox")
        # Do not force headless mode; interactive login is required.

        driver = None
        try:
            driver = webdriver.Chrome(options=chrome_options)
        except WebDriverException as exc:
            raise RuntimeError(
                "Unable to start the Chrome WebDriver. Ensure the Chrome browser "
                "and compatible chromedriver are installed and available in PATH."
            ) from exc

        try:
            login_url = os.environ.get(
                "OREILLY_LOGIN_URL", "https://learning.oreilly.com/"
            )
            self.logger.warning(
                f"Cookie file not found. Launching {browser_name} to collect cookies...",
            )
            driver.get(login_url)
            if self._verbose:
                self.logger.info(
                    "Please complete the login in the opened browser window. "
                    "After successfully logging in and verifying access to your library, "
                    "return here and press Enter to continue.",
                )
            text_input()

            cookies = driver.get_cookies()
            if not cookies:
                raise RuntimeError(
                    "No cookies were captured from the Selenium session. "
                    "Ensure you have logged in before continuing."
                )
            self.logger.info(
                f"Captured {len(cookies)} cookies. Saving to {path}.",
            )
            return cookies
        finally:
            if driver is not None:
                # Give the user a moment to read any final prompts before closing.
                time.sleep(1)
                driver.quit()


def main() -> int:
    """Setup logging and collect arguments for our application."""
    usage_doc = __doc__ or ""
    if sys.platform != "darwin":
        usage_doc = re.sub(r"\s*\[--browser \| ISBN\.\.\.\]", " [ISBN...]", usage_doc)
        usage_doc = re.sub(
            r"^\s*--browser\s+.*\n",
            "",
            usage_doc,
            flags=re.MULTILINE,
        )

    arguments = docopt(usage_doc)
    general.setup_logging(options=arguments)
    kw_args = docopt_arguments(
        arguments, all_args=True, logger=getLogger("oreilly-library")
    )
    app = oreilly_loader(**kw_args)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
