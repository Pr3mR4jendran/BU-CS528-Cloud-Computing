"""Hand-calculated checks for file parsing, aggregation, and ranking."""

from contextlib import redirect_stdout
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hw7 import main, read_results, summarize


FIXTURE_PAGES = (
    '<html><head><title>ignore these words</title></head><body>'
    '<p>RED, blue</p><p>red blue red blue</p>'
    '<a HREF="1.html"></a><a href="1.html"></a>'
    '<a href="0.html"></a><a href="3.html"></a>'
    '<script>ignore these words</script><style>ignore these words</style></body></html>',
    '<body>red <strong>bl</strong>ue red <a href="0.html">Green leaf</a></body>',
    '<body>red blue</body>',
    '<body>yellow gold black<a href="3.html"></a></body>',
    '<body>blue red violet<a href="4.html"></a></body>',
    '<body><a href="5.html"></a></body>',
    '<body><a href="6.html"></a></body>',
)

FIXTURE_RESULTS = {
    "incoming": [
        {"page": "0.html", "count": 2},
        {"page": "1.html", "count": 2},
        {"page": "3.html", "count": 2},
        {"page": "4.html", "count": 1},
        {"page": "5.html", "count": 1},
    ],
    "outgoing": [
        {"page": "0.html", "count": 4},
        {"page": "1.html", "count": 1},
        {"page": "3.html", "count": 1},
        {"page": "4.html", "count": 1},
        {"page": "5.html", "count": 1},
    ],
    "trigrams": [
        {"trigram": "red blue red", "count": 3},
        {"trigram": "blue red blue", "count": 2},
        {"trigram": "blue red green", "count": 1},
        {"trigram": "blue red violet", "count": 1},
        {"trigram": "red green leaf", "count": 1},
    ],
    "summary": {"files": 7, "incoming_links": 9, "outgoing_links": 9},
}


def write_fixture(directory, pages=FIXTURE_PAGES):
    directory = Path(directory)
    directory.mkdir()
    for index, html in enumerate(pages):
        (directory / f"{index}.html").write_text(html, encoding="utf-8")


class PipelineTests(unittest.TestCase):
    def run_pages(self, pages):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture(root / "pages", pages)
            with redirect_stdout(io.StringIO()):
                main([
                    "--runner=DirectRunner",
                    "--input", (root / "pages" / "*.html").as_posix(),
                    "--expected-files", str(len(pages)),
                    "--output", (root / "results").as_posix(),
                ])
            return read_results(str(root / "results"))

    def test_ranked_fixture(self):
        self.assertEqual(self.run_pages(FIXTURE_PAGES), FIXTURE_RESULTS)

    def test_zero_degree_and_empty_trigrams(self):
        result = self.run_pages(('<body>one two</body>', '<body><a href="1.html">three</a></body>'))
        expected_links = [{"page": "1.html", "count": 1}, {"page": "0.html", "count": 0}]
        self.assertEqual(result["incoming"], expected_links)
        self.assertEqual(result["outgoing"], expected_links)
        self.assertEqual(result["trigrams"], [])
        self.assertEqual(result["summary"], {"files": 2, "incoming_links": 1, "outgoing_links": 1})

    def test_duplicate_id_rejected_when_file_count_matches(self):
        with self.assertRaisesRegex(ValueError, "distinct IDs"):
            summarize([(0, 0), (0, 0)], 0, 2)


if __name__ == "__main__":
    unittest.main()
