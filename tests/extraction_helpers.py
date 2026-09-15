"""Functions run *inside* the extraction process by tests — M3/S3.3.

`PyMuPDFTextExtractor` pickles the function it runs across a process boundary,
which only works for functions importable by module path. These are those
functions. Each takes the extractor's `(data, max_pages)` arguments, and some
put them to unusual use, because that is all the child process receives.
"""

import os
import time
from pathlib import Path

from shared.models.extraction import ExtractedPage, ExtractedText


def record_pid_then_sleep(data: bytes, max_pages: int) -> ExtractedText:
    """Write this process's id to the path in `data`, then sleep `max_pages` seconds.

    Stands in for a hostile PDF that makes MuPDF spin: it holds the process for
    as long as it is allowed to, and reports which process it was.
    """
    Path(data.decode()).write_text(str(os.getpid()))
    time.sleep(max_pages)
    raise AssertionError("should have been killed before waking")


def die(data: bytes, max_pages: int) -> ExtractedText:
    """The process ends abruptly, as it would on a crash inside MuPDF."""
    os._exit(1)


def report_pid(data: bytes, max_pages: int) -> ExtractedText:
    """Write this process's id to the path in `data` and return nothing useful."""
    Path(data.decode()).write_text(str(os.getpid()))
    return ExtractedText(page_count=1, pages=(ExtractedPage(page_number=1),))
