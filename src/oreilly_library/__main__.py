"""
An application to download books from the O'Reilly Safari library for local
consumption. You will need a valid login for O'Reilly in order to do this.

Usage:
    oreilly-library [--verbose] [--debug] [--check] [--output-dir=OUTPUT] [--cookie-file=FILE] [--browser=BROWSER] [--login] ISBN...

Arguments:
    ISBN            Look for these books

Options:
    --output-dir=OUTPUT Put the output files here. [Default: working/Books]
    --cookie-file=FILE  Use cookies from FILE. If absent, start a Selenium login flow.
    --browser=BROWSER   Browser to use for Selenium login. [Default: chrome]
    --login             Force Selenium login to refresh cookies.
    --check             Run epubcheck validation after building each EPUB.
    --verbose           Make some noise
    --debug             Make a lot of noise
"""

import json
import os
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

from oreilly_library.epub_downloader import EpubDownloader

PATH = os.environ.get("SAFARIBOOKS_PATH") or "working"


class oreilly_loader:
    """Download an epub from O'Reilly Safari."""

    def __init__(
        self,
        isbns: Optional[Sequence[str]] = None,
        output_dir: Optional[str] = None,
        check: bool | None = None,
        verbose: bool | None = None,
        debug: bool | None = None,
        cookie_file: Optional[str] = None,
        browser: str | None = None,
        login: bool | None = None,
        **kwargs,
    ) -> None:
        if isinstance(isbns, str):
            isbns = [isbns]
        elif isinstance(isbns, Iterable):
            isbns = list(isbns)
        if not isbns:
            raise ValueError("No ISBN identifiers provided.")

        self._identifiers = list(isbns)
        self._output_dir = output_dir
        self._check = bool(check)
        self._verbose = bool(verbose)
        self._debug = bool(debug)
        self.logger = getLogger(self.__class__.__name__)
        cookies_env = os.environ.get("SAFARICOOKIES_PATH")
        self._cookies_file = Path(
            cookie_file or cookies_env or os.path.join(PATH, "cookies.json")
        )
        self._browser = (browser or "chrome").lower()
        self._force_login = bool(login)
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
        """Use the EpubDownloaer class to fetch an epub."""
        target_dir: Path
        if output_dir is not None:
            target_dir = Path(output_dir)
        elif self._output_dir is not None:
            target_dir = Path(self._output_dir)
        else:
            target_dir = Path(PATH) / "Books"

        downloader = EpubDownloader(
            identifier=identifier,
            session=self.session,
            output_dir=target_dir,
            check=self._check,
            verbose=self._verbose and not self._debug,
            debug=self._debug,
        )
        downloader.download_and_build()

    def run(self) -> int:
        if self._output_dir and not Path(self._output_dir).exists():
            Path(self._output_dir).mkdir(parents=True, exist_ok=True)
        if len(self._identifiers) > 1 and not self._verbose and not self._debug:
            iterator = tqdm(self._identifiers)
        else:
            iterator = self._identifiers
        for identifier in iterator:
            self.download_epub(identifier, output_dir=self._output_dir)
        return 0

    def _ensure_cookies(self) -> list[dict] | dict:
        if self._force_login:
            self.logger.info(
                "--login flag supplied. Launching Selenium to refresh cookies.",
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

        if self._browser != "chrome":
            raise RuntimeError(
                f"Unsupported browser '{self._browser}'. Only 'chrome' is currently supported."
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
                "Cookie file not found. Launching browser to collect cookies...",
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
    arguments = docopt(__doc__)
    general.setup_logging(options=arguments)
    kw_args = docopt_arguments(
        arguments, all_args=True, logger=getLogger("oreilly-library")
    )
    kw_args.setdefault("browser", "chrome")
    app = oreilly_loader(**kw_args)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
