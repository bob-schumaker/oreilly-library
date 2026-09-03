from unittest import TestCase
from unittest.mock import Mock, patch

from oreilly_library.__main__ import oreilly_loader
from oreilly_library.early_release_tracker import TrackedBook


class EarlyReleaseProgressTests(TestCase):
    def setUp(self) -> None:
        self.tracked_books = [
            TrackedBook("one", "First book", "2026-01-01"),
            TrackedBook("two", "Second book", "2026-01-01"),
        ]

    def _loader(self, *, verbose: bool, debug: bool) -> oreilly_loader:
        loader = object.__new__(oreilly_loader)
        loader._tracker = Mock(list_books=Mock(return_value=self.tracked_books))
        loader._verbose = verbose
        loader._debug = debug
        loader._clean = False
        loader._fetch_updates = False
        loader._initialize_session = Mock()
        loader._fetch_remote_book_metadata = Mock(return_value={"roughcut": True})
        return loader

    def test_verbose_check_uses_progress_bar(self) -> None:
        loader = self._loader(verbose=True, debug=False)

        with patch(
            "oreilly_library.__main__.tqdm", side_effect=lambda books: books
        ) as progress:
            loader._run_early_release_check()

        progress.assert_called_once_with(self.tracked_books)

    def test_debug_check_does_not_use_progress_bar(self) -> None:
        loader = self._loader(verbose=False, debug=True)

        with patch("oreilly_library.__main__.tqdm") as progress:
            loader._run_early_release_check()

        progress.assert_not_called()
