"""Local compatibility subset extracted from unpublished ``cobblerslib``.

This project used to depend on a private helper package named ``cobblerslib``.
That package is not published on PyPI, so the small amount of support used by
``oreilly-library`` has been flattened into this single file. The functions
below are copied from the original library's ``general.docopt``,
``general.logsupport``, ``general.datafiles``, ``general.utilities``, and
``general.texthandling`` modules, with internal imports replaced by local
references so the compatibility layer stays self-contained.
"""

from __future__ import annotations

import builtins
import collections
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from enum import IntEnum
from io import BufferedIOBase, RawIOBase, TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from random import randint
from types import TracebackType
from typing import Any, Callable, Dict, Iterable, List, Optional, Type, Union

from docopt import docopt as docopt_orig

US_MILSPEC_FORMAT = "%H%M{}%d%b%y"
MIL_STD_2500A_FORMAT = "%d%H%M{}%b%y"
DEFAULT_VERSION_FORMAT = "v{}"
ISO_8061_FILENAME_FORMAT = "%Y-%m-%d-%H%M%S"

IGNORE_BASECLIENT_LOGGING = {"werner.baseclient": logging.CRITICAL}
IGNORE_PARAMIKO_LOGGING = {"paramiko.transport": logging.CRITICAL}
IGNORE_REQUESTS_LOGGING = {"requests.packages.urllib3.connectionpool": logging.CRITICAL}
IGNORE_URLLIB_LOGGING = {"urllib3.connectionpool": logging.CRITICAL}
IGNORE_KEYRING_LOGGING = {"keyring.backend": logging.CRITICAL}


class TimeSpecFormat(IntEnum):
    """
    An enumeration that represents different time styles for special formatting.
    """

    # Represents the "UNAMBIGUOUS" time style.
    MILSPEC_FLAG = 1

    # Represents the "UNAMBIGUOUS+UTC" time style.
    MILSPEC_FLAG_UTC = 2

    # Represents the "Date Time Group" time style.
    DTG_TIMESPEC_FLAG = 3
    MIL_STD_2500A_FLAG = 3  # Alias for DTG_TIMESPEC_FLAG

    # Represents the "Date Time Group+UTC" time style.
    DTG_TIMESPEC_FLAG_UTC = 4
    MIL_STD_2500A_FLAG_UTC = 4  # Alias for DTG_TIMESPEC_FLAG_UTC

    # Represents the "ISO" time style.
    ISO_8061_FLAG = 5

    # Represents the "ISO+UTC" time style.
    ISO_8061_FLAG_UTC = 6


def read_json(
    handle: Union[TextIOWrapper, RawIOBase, BufferedIOBase],
    deserializer: Optional[Callable] = None,
    ordered: bool = False,
) -> Any:
    """Read a JSON file from a handle, convert from bytes if
    necessary."""
    object_pairs_hook = collections.OrderedDict if ordered else None
    data = json.load(handle, object_pairs_hook=object_pairs_hook)
    if deserializer:
        return deserializer(data)
    return data


def load_json(
    filename: Union[Path, str],
    deserializer: Optional[Callable] = None,
    ordered: bool = False,
) -> Any:
    """
    Utility function to load json from a file.

    Specify ordered=True if you want your input ordering to be maintained.
    """
    if Path(filename).exists():
        with open(str(filename), "rt", encoding="utf-8") as handle:
            return read_json(handle, ordered=ordered, deserializer=deserializer)
    return None


def military_datetime(
    time_flag: TimeSpecFormat = TimeSpecFormat.MILSPEC_FLAG_UTC,
    mark: Optional[datetime] = None,
) -> str:
    """Get the time in military format.

    >>> from utilities import military_datetime, TimeSpecFormat
    >>> from datetime import datetime, timezone
    >>> print(military_datetime(mark=datetime(2024, 2, 16, 19, 37, 18, 955528)))
    1937Z16FEB24
    >>> print(military_datetime(mark=datetime(2024, 2, 16, 19, 37, 18, 955528, tzinfo=timezone.utc)))
    1937Z16FEB24
    >>> print(military_datetime(time_flag=TimeSpecFormat.MIL_STD_2500A_FLAG, mark=datetime(2024, 2, 16, 19, 37, 18, 955528)))
    161937UFEB24
    >>> print(military_datetime(time_flag=TimeSpecFormat.MIL_STD_2500A_FLAG, mark=datetime(2024, 2, 16, 19, 37, 18, 955528, tzinfo=timezone.utc)))
    161937ZFEB24
    >>> print(military_datetime(time_flag=TimeSpecFormat.MIL_STD_2500A_FLAG_UTC, mark=datetime(2024, 2, 16, 19, 37, 18, 955528)))
    161937ZFEB24
    >>> print(military_datetime(time_flag=TimeSpecFormat.MIL_STD_2500A_FLAG_UTC, mark=datetime(2024, 2, 16, 19, 37, 18, 955528, tzinfo=timezone.utc)))
    161937ZFEB24
    """
    if not mark:
        mark = datetime.now()
    if mark.tzinfo is None or mark.tzinfo.utcoffset(mark) is None:
        if time_flag in (
            TimeSpecFormat.MILSPEC_FLAG_UTC,
            TimeSpecFormat.MIL_STD_2500A_FLAG_UTC,
        ):
            mark = mark.replace(tzinfo=timezone.utc)
        else:
            now = datetime.now().astimezone()
            mark = mark.astimezone(tz=now.tzinfo)
    utc_offset = mark.utcoffset()
    # If we didn't get a timezone aware
    if utc_offset:
        utc_offset = int(utc_offset.total_seconds()) // 3600
        if utc_offset == -13:
            utc_offset = 1
    else:
        utc_offset = 0
    if utc_offset == 0:
        zone = "Z"
    else:
        if utc_offset < 0:
            zone = chr(ord("M") - utc_offset)
        else:
            zone = chr(ord("A") + utc_offset)
    if time_flag in (
        TimeSpecFormat.MIL_STD_2500A_FLAG,
        TimeSpecFormat.MIL_STD_2500A_FLAG_UTC,
    ):
        time_format = MIL_STD_2500A_FORMAT
    else:
        time_format = US_MILSPEC_FORMAT
    return mark.strftime(time_format.format(zone)).upper()


def get_versioned_path(
    filepath: str,
    version_format: Optional[str] = None,
    include_date: Optional[Union[bool, str, TimeSpecFormat]] = None,
    postfix: Optional[str] = None,
    first_dash: bool = True,
    cutoff: int = 100,
) -> str:
    """Get a unique filename in the data folder. File names are formed as
        filename[-today][-postfix][-uniquifier]
    if firstdash is False and include_date is True:
        filename[ today][-postfix][-uniquifier]
    """
    path = Path(filepath)
    if path.parent:
        path.parent.mkdir(exist_ok=True)
    base_name = path.stem.rstrip("-")
    addendum = ""
    if include_date:
        if isinstance(include_date, bool):
            include_date = TimeSpecFormat.ISO_8061_FLAG_UTC
        if include_date in (
            TimeSpecFormat.MILSPEC_FLAG,
            TimeSpecFormat.MILSPEC_FLAG_UTC,
            TimeSpecFormat.DTG_TIMESPEC_FLAG,
            TimeSpecFormat.DTG_TIMESPEC_FLAG_UTC,
        ):
            addendum += f"-{military_datetime(time_flag=include_date)}"
        elif include_date == TimeSpecFormat.ISO_8061_FLAG:
            addendum += f"-{datetime.now().strftime(ISO_8061_FILENAME_FORMAT)}"
        elif include_date == TimeSpecFormat.ISO_8061_FLAG_UTC:
            addendum += f"-{datetime.utcnow().strftime(ISO_8061_FILENAME_FORMAT)}"
        elif isinstance(include_date, str):
            addendum += f"-{datetime.utcnow().strftime(include_date)}"
    elif not version_format:
        # Make sure we have something to distnguish the files.
        version_format = DEFAULT_VERSION_FORMAT

    if postfix:
        addendum += f"-{postfix.strip('-')}"
    if version_format:
        addendum += f"-{version_format.strip('-')}"
    if not first_dash:
        addendum = f" {addendum.strip('-')}"
    for idx in range(1, cutoff + 1):
        check_name = f"{base_name}{addendum.format(idx)}{path.suffix}"
        check_path = path.with_name(check_name)
        if not check_path.exists():
            return str(check_path)
    check_name = f"{base_name}{addendum.format(randint(1000, 10000))}{path.suffix}"
    return str(path.with_name(check_name))


def _get_option(options: Any, option: str) -> Optional[Any]:
    """Do we have an option?"""
    if options:
        if isinstance(options, dict):
            return options.get(f"--{option}") or options.get(option)
        elif hasattr(options, option):
            return getattr(options, option)
    return None


def _dict_options(options: Any, *keys: str) -> Dict[str, Any]:
    """Get all of the options, if they're there."""
    result = {}
    for key in keys:
        value = _get_option(options, key)
        if value is not None:
            result[key] = value
    return result


def add_file_handler(
    log_path: Union[Path, str],
    options: Optional[Any] = None,
    log_format: Optional[str] = None,
    log_level: int = logging.INFO,
) -> None:
    """Add a file handler."""
    logging_config = _dict_options(
        options, "mode", "maxBytes", "backupCount", "encoding", "errors", "delay"
    )
    if "backupCount" in logging_config or "maxBytes" in logging_config:
        handler = RotatingFileHandler(str(log_path), **logging_config)
    else:
        log_path = get_versioned_path(str(log_path))
        handler = logging.FileHandler(log_path, **logging_config)
    handler.setFormatter(
        logging.Formatter(
            log_format or "%(asctime)s:%(levelname)s:%(name)s:%(message)s"
        )
    )
    handler.setLevel(log_level)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)


def setup_logging(
    default_path: str = "logging.json",
    default_level: int = logging.INFO,
    env_key: str = "LOG_CFG",
    module_overrides: Optional[Dict[str, int]] = None,
    options: Optional[Any] = None,
    default_logfile: Optional[str] = None,
    debug: bool = False,
):
    """
    Setup logging configuration

    https://fangpenlin.com/posts/2012/08/26/good-logging-practice-in-python/

        :param default_path='logging.json':
            Logging configuration JSON file.
        :param default_level=logging.INFO:
            Default logging level.
        :param env_key='LOG_CFG':
            Environment variable pointing to configuration JSON file.
        :param module_overrides=None:
            A dictionary of "module name": "logging level" to change
            module logging values.
    """
    if debug or _get_option(options, "debug"):
        default_level = logging.DEBUG

    path = default_path
    value = os.getenv(env_key, None)
    if value:
        path = value
    if os.path.exists(path):
        config = load_json(path)
        logging.basicConfig(**config)
    else:
        logging.basicConfig(level=default_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(default_level)
    if module_overrides:
        for module, log_level in module_overrides.items():
            logging.getLogger(module).setLevel(log_level)

    if _get_option(options, "gmt") or _get_option(options, "utc"):
        logging.Formatter.converter = time.gmtime

    if options:
        log_path = None
        for key in ("log", "logfile", "filename"):
            log_path = _get_option(options, key)
            if log_path:
                break
        if log_path is True and default_logfile:
            log_path = default_logfile
        if isinstance(log_path, (str, Path)):
            add_file_handler(
                log_path, options=options, log_format=_get_option(options, "log_format")
            )


def default_overrides(
    logging_overrides: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Return the overrides we know we usually want."""
    if not logging_overrides:
        logging_overrides = {}
    logging_overrides.update(IGNORE_BASECLIENT_LOGGING)
    logging_overrides.update(IGNORE_PARAMIKO_LOGGING)
    logging_overrides.update(IGNORE_REQUESTS_LOGGING)
    logging_overrides.update(IGNORE_URLLIB_LOGGING)
    logging_overrides.update(IGNORE_KEYRING_LOGGING)
    return logging_overrides


def formatted_exception(
    exctype: Type[BaseException],
    value: BaseException,
    tb: Optional[TracebackType] = None,
) -> str:
    """Format the exceptionm for display."""
    traceback_formatted = traceback.format_exception(exctype, value=value, tb=tb)
    return "".join(traceback_formatted)


class _General:
    """Compatibility namespace for the former ``cobblerslib.general`` module."""

    setup_logging = staticmethod(setup_logging)


general = _General()


def docopt_arguments(
    arguments: Dict[str, Any],
    ignore: Optional[Iterable[str]] = None,
    all_args: bool = False,
    **kw_args,
) -> Dict:
    """Assume we want a bunch of constructor arguments with the same names as
    our '--' options, without the '--'.
    """
    option_args = {}
    for option, value in arguments.items():
        if "--" not in option:
            # Handle uppesrcase options as arguments that will be
            # passed in as the lowercase version.
            if not all_args or option.upper() != option:
                continue
            option = option.lower()
            if isinstance(value, list):
                option += "s"
        option = option.strip("-")
        option = option.replace("-", "_")
        if ignore and option in ignore:
            continue
        option_args[option] = value
    if kw_args:
        option_args.update(kw_args)
    return option_args


def docopt(
    docstring: Optional[str] = None,
    command_name: Optional[str] = None,
    default_args: Optional[Union[Iterable[str], str, bool]] = None,
) -> Dict[str, Any]:
    """Insert the default_args after every command. It is assumed that all lowercase
    items are 'verbs' and that all non '--' options are in uppercase."""

    if not docstring:
        return {}

    if default_args is None:
        return docopt_orig(docstring)

    if not command_name:
        command_name = os.path.basename(sys.argv[0])

    if default_args is True:
        default_args = ["--verbose", "--debug"]
    elif isinstance(default_args, str):
        default_args = [default_args]

    def _append_default_args(new_line: List[str], indent: str) -> None:
        """Append the default args to the specified list."""
        if default_args:
            for default_arg in default_args:
                new_line.append(
                    f"\n{indent}{indent}{default_arg}"
                    if "[" in default_arg
                    else f"[{default_arg}]"
                )

    modified_doc = []
    process_lines = True
    for line in docstring.splitlines():
        orig_line = line
        line = line.strip()
        indent = ""
        if line:
            chop = slice(0, len(orig_line) - len(line))
            indent = orig_line[chop]
            line_l = line.lower()
            if process_lines and (
                line_l.startswith("options") or line_l.startswith("arguments")
            ):
                process_lines = False
            if process_lines and line.startswith(command_name):
                new_line = [command_name]
                words = line.split()[1:]
                inserted = False
                for word in words:
                    if inserted or word in ("[]|()"):
                        new_line.append(word)
                    elif word.lower() != word or "--" in word:
                        _append_default_args(new_line, indent)
                        new_line.append(word)
                        inserted = True
                    else:
                        new_line.append(word)
                if not inserted:
                    _append_default_args(new_line, indent)
                line = " ".join(new_line)
        modified_doc.append(indent + line)
    return docopt_orig("\n".join(modified_doc))


def docopt_verb(arguments: Dict[str, Any]) -> str:
    """Look for something that looks like a verb of the form:
    command-invocation verb
    """
    for arg, value in arguments.items():
        if not arg.startswith("-") and arg.lower() == arg and value:
            return arg
    return ""


def text_input(__prompt: object = "") -> str:
    """Run the input() builtin without triggering fortify, since
    we're not using python2."""
    return getattr(builtins, "input")(__prompt)
