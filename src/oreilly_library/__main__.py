"""
An application to download books from the O'Reilly Safari library for local
consumption. You will need a valid login for O'Reilly in order to do this.

Usage:
    oreilly-library [--verbose] [--debug] [--check] [--clean] [--build] [--output-dir=OUTPUT] [--cookie-file=FILE] [--login=BROWSER] [--browser | ISBN...]

Arguments:
    ISBN            Look for these books

Options:
    --browser           Get the open URLs from the browser and load the ones that are Safari books
    --build             Build EPUBs from previously downloaded local files without downloading again
    --output-dir=OUTPUT Put the output files here. [Default: working/Books]
    --cookie-file=FILE  Use cookies from FILE. If absent, start a Selenium login flow.
    --login=BROWSER     Force Selenium login to refresh cookies using BROWSER.
    --check             Run epubcheck validation after building each EPUB.
    --clean             Run Calibre ebook-polish cleanup after building each EPUB.
    --verbose           Make some noise
    --debug             Make a lot of noise
"""

import json
import os
import re
import sys
import time
from logging import getLogger
from pathlib import Path
from typing import Iterable, Optional, Sequence

import requests
from cobblerslib import general
from cobblerslib.general.docopt import docopt, docopt_arguments
from cobblerslib.general.texthandling import text_input
from tqdm import tqdm

if sys.platform == "darwin":
    from appscript import app

from oreilly_library.epub_builder import EpubBuilder
from oreilly_library.epub_downloader import EpubDownloader

PATH = os.environ.get("SAFARIBOOKS_PATH") or "working"


class oreilly_loader:
    """Download an epub from O'Reilly Safari."""

    def __init__(
        self,
        isbns: Optional[Sequence[str]] = None,
        output_dir: Optional[str] = None,
        check: bool | None = None,
        clean: bool | None = None,
        build: bool | None = None,
        verbose: bool | None = None,
        debug: bool | None = None,
        cookie_file: Optional[str] = None,
        browser: str | None = None,
        login: str | bool | None = None,
        **kwargs,
    ) -> None:
        if isinstance(isbns, str):
            isbns = [isbns]
        elif isinstance(isbns, Iterable):
            isbns = list(isbns)
        if not isbns:
            if not browser:
                raise ValueError("No ISBN identifiers or --browser flag provided.")
            self._identifiers = []
        else:
            self._identifiers = list(isbns)
        self._output_dir = output_dir
        self._check = bool(check)
        self._clean = bool(clean)
        self._build = bool(build)
        self._browser = browser
        self._verbose = bool(verbose)
        self._debug = bool(debug)
        self.logger = getLogger(self.__class__.__name__)
        cookies_env = os.environ.get("SAFARICOOKIES_PATH")
        self._cookies_file = Path(
            cookie_file or cookies_env or os.path.join(PATH, "cookies.json")
        )
        self._login = self._resolve_login_configuration(
            login=login,
        )
        self.session: requests.Session | None = None
        if not self._build:
            self.session = requests.Session()
            cookie_data = self._ensure_cookies()
            self._apply_cookie_data(cookie_data)
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate",
                "Referer": "https://learning.oreilly.com/login/unified/?next=/home/",
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/90.0.4430.212 Safari/537.36",
            }
            self.session.headers.update(headers)

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
                check=self._check,
                verbose=self._verbose and not self._debug,
                debug=self._debug,
            )
            builder.build_epub(clean=self._clean)
            return

        if self.session is None:
            raise RuntimeError("Authenticated session was not initialized.")

        downloader = EpubDownloader(
            identifier=identifier,
            session=self.session,
            output_dir=target_dir,
            check=self._check,
            clean=self._clean,
            verbose=self._verbose and not self._debug,
            debug=self._debug,
        )
        downloader.download_and_build()

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

    def run(self) -> int:
        if self._output_dir and not Path(self._output_dir).exists():
            Path(self._output_dir).mkdir(parents=True, exist_ok=True)
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

        if self._login != "chrome":
            raise RuntimeError(
                f"Unsupported browser '{self._login}'. Only 'chrome' is currently supported."
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
                f"Cookie file not found. Launching {self._login} to collect cookies...",
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
