#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

""" warc2zim conversion utility

This utility provides a conversion from WARC records to ZIM files.
The WARCs are converted in a 'lossless' way, no data from WARC records is lost.
Each WARC record results in two ZIM articles:
- The WARC payload is stored under /A/<url>
- The WARC headers + HTTP headers are stored under the /H/<url>

Given a WARC response record for 'https://example.com/', two ZIM articles are created /A/example.com/ and /H/example.com/ are created.

Only WARC response and resource records are stored.

If the WARC contains multiple entries for the same URL, only the first entry is added, and later entries are ignored. A warning is printed as well.

"""

from argparse import ArgumentParser, RawTextHelpFormatter
import os
import logging
import mimetypes
import pkg_resources
import requests

from warcio import ArchiveIterator
from libzim.writer import Article, Blob
from zimscraperlib.zim.creator import Creator
from bs4 import BeautifulSoup


# Shared logger
logger = logging.getLogger("warc2zim")


# ============================================================================
class BaseArticle(Article):
    """ BaseArticle for all ZIM Articles in warc2zim with default settings
    """

    def is_redirect(self):
        return False

    def get_title(self):
        return ""

    def get_filename(self):
        return ""

    def should_compress(self):
        return True

    def should_index(self):
        return False


# ============================================================================
class BaseWARCArticle(BaseArticle):
    """ BaseWARCArticle that produces ZIM articles from WARC records
    """

    def __init__(self, record):
        super(BaseWARCArticle, self).__init__()
        self.record = record


# ============================================================================
class WARCHeadersArticle(BaseWARCArticle):
    """ WARCHeadersArticle used to store the WARC + HTTP headers as text
    Usually stored under H namespace
    """

    def __init__(self, record):
        super(WARCHeadersArticle, self).__init__(record)
        self.url = record.rec_headers.get("WARC-Target-URI")

    def get_url(self):
        return "H/" + canonicalize(self.url)

    def get_title(self):
        return self.url

    def get_mime_type(self):
        return "application/warc-headers"

    def get_data(self):
        # add WARC headers
        buff = self.record.rec_headers.to_bytes(encoding="utf-8")
        # add HTTP headers, if present
        if self.record.http_headers:
            buff += self.record.http_headers.to_bytes(encoding="utf-8")

        return Blob(buff)


# ============================================================================
class WARCPayloadArticle(BaseWARCArticle):
    """ WARCPayloadArticle used to store the WARC payload
    Usually stored under A namespace
    """

    def __init__(self, record):
        super(WARCPayloadArticle, self).__init__(record)
        self.url = record.rec_headers.get("WARC-Target-URI")
        self.mime = self._compute_mime()
        self.title = self.url
        self.payload = self.record.content_stream().read()

        # TODO: converting text/html to text/unchanged-html to avoid rewriting by kiwix
        # original mime type still preserved in the headers block
        self.mime = self.mime.split(";", 1)[0]
        if self.mime == "text/html":
            self.mime = "text/unchanged-html"
            self.title = self._parse_title()

    def _parse_title(self):
        soup = BeautifulSoup(self.payload, "html.parser")
        try:
            return soup.title.string or self.url
        except AttributeError:
            return self.url

    def _compute_mime(self):
        if self.record.http_headers:
            # if the record has HTTP headers, use the Content-Type from those (eg. 'response' record)
            return self.record.http_headers["Content-Type"]

        # otherwise, use the Content-Type from WARC headers
        return self.record.rec_headers["Content-Type"]

    def get_url(self):
        return "A/" + canonicalize(self.url)

    def get_title(self):
        return self.title

    def get_mime_type(self):
        return self.mime

    def get_data(self):
        return Blob(self.payload)

    def should_index(self):
        return True


# ============================================================================
class RWPRemoteArticle(BaseArticle):
    def __init__(self, prefix, filename):
        super(RWPRemoteArticle, self).__init__()
        self.prefix = prefix
        self.filename = filename

        try:
            resp = requests.get(self.prefix + filename)
            self.content = resp.content
            self.mime = resp.headers.get("Content-Type").split(";")[0]
        except Exception as e:
            logger.error(e)
            logger.error(
                "Unable to load replay system file: {0}".format(self.prefix + filename)
            )
            raise

    def get_url(self):
        return "A/" + self.filename

    def get_mime_type(self):
        return self.mime

    def get_data(self):
        return Blob(self.content)


# ============================================================================
class RWPStaticArticle(BaseArticle):
    def __init__(self, filename, main_url):
        super(RWPStaticArticle, self).__init__()
        self.filename = filename
        self.main_url = main_url

        self.mime, _ = mimetypes.guess_type(filename)
        self.content = pkg_resources.resource_string(
            "warc2zim", "replay/" + filename
        ).decode("utf-8")

    def get_url(self):
        return "A/" + self.filename

    def get_mime_type(self):
        return self.mime

    def get_data(self):
        if self.mime == "text/html":
            content = self.content.replace("$MAIN_URL", self.main_url)
        else:
            content = self.content
        return Blob(content.encode("utf-8"))


# ============================================================================
class WARC2Zim:
    def __init__(self, args):
        logging.basicConfig(format="[%(levelname)s] %(message)s")
        if args.verbose:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

        self.indexed_urls = set({})
        self.output = args.output
        if not self.output:
            self.output, ext = os.path.splitext(args.inputs[0])
            self.output += ".zim"

        self.inputs = args.inputs
        self.replay_viewer_source = args.replay_viewer_source
        self.main_url = args.url

        self.language = args.lang

        print(args.tags)

        self.metadata = {
            "name": args.name,
            "title": args.title,
            "description": args.desc,
            "publisher": args.publisher,
            "tags": ";".join(args.tags) or None,
            "source": args.source,
            "flavour": "fromWARC",
            "scraper": "warc2zim " + get_version(),
        }

        self.replay_articles = []
        self.revisits = {}

    def add_remote_or_local(self, filename):
        if self.replay_viewer_source:
            article = RWPRemoteArticle(self.replay_viewer_source, filename)
        else:
            article = RWPStaticArticle(filename, self.main_url)

        self.replay_articles.append(article)

    def run(self):
        self.add_remote_or_local("sw.js")

        for filename in pkg_resources.resource_listdir("warc2zim", "replay"):
            if filename != "sw.js":
                self.replay_articles.append(RWPStaticArticle(filename, self.main_url))

        with Creator(
            self.output, main_page="index.html", language=self.language
        ) as zimcreator:

            for article in self.generate_all_articles():
                zimcreator.add_zim_article(article)

            zimcreator.update_metadata(**self.metadata)

    def generate_all_articles(self):
        # add replay system
        for article in self.replay_articles:
            yield article

        for warcfile in self.inputs:
            yield from self.generate_warc_articles(warcfile)

        # process revisits, headers only
        for url, record in self.revisits.items():
            if url not in self.indexed_urls:
                logger.debug(
                    "Adding revisit {0} -> {1}".format(
                        url, record.rec_headers["WARC-Refers-To-Target-URI"]
                    )
                )
                yield WARCHeadersArticle(record)
                self.indexed_urls.add(url)

    def generate_warc_articles(self, warcfile):
        with open(warcfile, "rb") as warc_fh:
            for record in ArchiveIterator(warc_fh):
                if record.rec_type not in ("resource", "response", "revisit"):
                    continue

                url = record.rec_headers["WARC-Target-URI"]
                if url in self.indexed_urls:
                    logger.warning(
                        "Skipping duplicate {0}, already added to ZIM".format(url)
                    )
                    continue

                if record.rec_type != "revisit":
                    yield WARCHeadersArticle(record)
                    payload_article = WARCPayloadArticle(record)

                    if len(payload_article.payload) != 0:
                        yield payload_article

                    self.indexed_urls.add(url)

                elif (
                    record.rec_headers["WARC-Refers-To-Target-URI"] != url
                    and url not in self.revisits
                ):
                    self.revisits[url] = record


# ============================================================================
def warc2zim(args=None):
    parser = ArgumentParser(description="Create ZIM files from WARC files")

    parser.add_argument("-V", "--version", action="version", version=get_version())
    parser.add_argument("-v", "--verbose", action="store_true")

    parser.add_argument(
        "inputs",
        nargs="+",
        help="""Paths of directories and/or files to be included in
                                the WARC file.""",
    )

    parser.add_argument(
        "-o",
        "--output",
        help="""Output filename for ZIM file (.zim extension will be added)""",
        metavar="output",
    )

    parser.add_argument(
        "-r",
        "--replay-viewer-source",
        help="""URL from which to load the ReplayWeb.page replay viewer from""",
    )

    parser.add_argument(
        "-u",
        "--url",
        help="""The main url that should be loaded in the viewer on init""",
        default="https://example.com/",
    )

    # optional metadata
    parser.add_argument("--name", help="The name of the ZIM", default="")
    parser.add_argument("--title", help="The Title", default="")
    parser.add_argument("--desc", help="The Description", default="")
    parser.add_argument("--tags", action="append", help="One or more tags", default=[])
    parser.add_argument("--lang", help="Language", default="eng")
    parser.add_argument("--publisher", help="ZIM publisher", default="-")
    parser.add_argument("--source", help="ZIM source", default="-")

    r = parser.parse_args(args=args)
    warc2zim = WARC2Zim(r)
    return warc2zim.run()


# ============================================================================
def canonicalize(url):
    """ Return a 'canonical' version of the url under which it is stored in the ZIM
    For now, just removing the scheme
    """
    return url.split("//", 2)[1]


# ============================================================================
def get_version():
    return "%(prog)s " + pkg_resources.get_distribution("warc2zim").version


# ============================================================================
if __name__ == "__main__":  # pragma: no cover
    warc2zim()
