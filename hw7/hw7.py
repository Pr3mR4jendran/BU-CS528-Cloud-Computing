"""Rank links and word trigrams in the numbered HW-2 HTML pages with Beam."""

import argparse
from collections import Counter
from html.parser import HTMLParser
import json
import logging
import os
import platform
import re
import time

import apache_beam as beam
from apache_beam.io import fileio
from apache_beam.io.filesystems import FileSystems
from apache_beam.options.pipeline_options import PipelineOptions, StandardOptions
from apache_beam.runners.runner import PipelineState


PAGE_NAME = re.compile(r"(0|[1-9][0-9]*)\.html")
WORD = re.compile(r"[A-Za-z]+")
BLOCK_TAGS = {
    "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "li", "p", "pre", "section", "td", "th", "tr",
}
RESULT_NAMES = ("incoming", "outgoing", "trigrams", "summary")


def page_id(filename, expected_files):
    match = PAGE_NAME.fullmatch(filename)
    if match is None or int(match[1]) >= expected_files:
        raise ValueError(f"Expected a page ID in 0..{expected_files - 1}, got {filename!r}")
    return int(match[1])


class PageParser(HTMLParser):
    def __init__(self, expected_files):
        super().__init__(convert_charrefs=True)
        self.expected_files = expected_files
        self.targets = Counter()
        self.text = []
        self.in_body = False
        self.skip_text = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.targets[page_id(href, self.expected_files)] += 1
        if tag == "body":
            self.in_body = True
        if tag in {"script", "style"}:
            self.skip_text = True
        if self.in_body and tag in BLOCK_TAGS:
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False
        if tag in {"script", "style"}:
            self.skip_text = False
        if self.in_body and tag in BLOCK_TAGS:
            self.text.append(" ")

    def handle_data(self, data):
        if self.in_body and not self.skip_text:
            self.text.append(data)


class CountPage(beam.DoFn):
    def __init__(self, expected_files):
        self.expected_files = expected_files

    def process(self, readable_file):
        path = readable_file.metadata.path
        source = page_id(os.path.basename(path), self.expected_files)
        parser = PageParser(self.expected_files)
        try:
            with readable_file.open() as file:
                parser.feed(file.read().decode("utf-8"))
            parser.close()
        except ValueError as error:
            raise ValueError(f"Invalid HTML input {path!r}: {error}") from error

        yield source, sum(parser.targets.values())
        # Emit a key for every input page, including pages nobody links to.
        parser.targets.setdefault(source, 0)
        for target, count in parser.targets.items():
            yield beam.pvalue.TaggedOutput("incoming", (target, count))

        words = WORD.findall("".join(parser.text).lower())
        trigrams = Counter(zip(words, words[1:], words[2:]))
        for words_in_trigram, count in trigrams.items():
            yield beam.pvalue.TaggedOutput("trigrams", (" ".join(words_in_trigram), count))


def rank_five(counts, label, field, format_key):
    return (
        counts
        | f"Top five {label}" >> beam.combiners.Top.Of(
            5, key=lambda row: (-row[1], row[0]), reverse=True
        )
        | f"Format {label}" >> beam.Map(
            lambda rows: [{field: format_key(key), "count": count} for key, count in rows]
        )
    )


def summarize(outgoing_rows, incoming_total, expected_files):
    page_ids = sorted(page for page, _ in outgoing_rows)
    if page_ids != list(range(expected_files)):
        raise ValueError(
            f"Expected one HTML file per ID from 0 through {expected_files - 1}. "
            f"Read {len(page_ids)} files with {len(set(page_ids))} distinct IDs."
        )
    outgoing_total = sum(count for _, count in outgoing_rows)
    if incoming_total != outgoing_total:
        raise ValueError(f"Link totals differ: {incoming_total} incoming, {outgoing_total} outgoing")
    return {"files": len(page_ids), "incoming_links": incoming_total, "outgoing_links": outgoing_total}


def build_outputs(pipeline, input_glob, expected_files):
    pages = (
        pipeline
        | "Match HTML files" >> fileio.MatchFiles(
            input_glob, empty_match_treatment=fileio.EmptyMatchTreatment.DISALLOW
        )
        | "Distribute file metadata" >> beam.Reshuffle()
        | "Open HTML matches" >> fileio.ReadMatches()
        | "Count each page" >> beam.ParDo(CountPage(expected_files)).with_outputs(
            "incoming", "trigrams", main="outgoing"
        )
    )
    incoming = pages.incoming | "Sum incoming by target" >> beam.CombinePerKey(sum)
    trigrams = pages.trigrams | "Sum occurrences by trigram" >> beam.CombinePerKey(sum)
    incoming_total = (
        incoming
        | "Extract incoming counts" >> beam.Values()
        | "Total incoming links" >> beam.CombineGlobally(sum)
    )
    summary = (
        pages.outgoing
        | "Collect per-page outgoing counts" >> beam.combiners.ToList()
        | "Check file IDs and link totals" >> beam.Map(
            summarize, beam.pvalue.AsSingleton(incoming_total), expected_files
        )
    )
    return {
        "incoming": rank_five(incoming, "incoming", "page", lambda page: f"{page}.html"),
        "outgoing": rank_five(pages.outgoing, "outgoing", "page", lambda page: f"{page}.html"),
        "trigrams": rank_five(trigrams, "trigrams", "trigram", str),
        "summary": summary,
    }


def read_results(directory):
    results = {}
    for name in RESULT_NAMES:
        with FileSystems.open(FileSystems.join(directory, f"{name}.json")) as file:
            results[name] = json.load(file)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--input", required=True, help="Quoted glob for the numbered HTML files")
    parser.add_argument("--output", required=True, help="Fresh local directory or gs:// output prefix")
    parser.add_argument("--expected-files", type=int, default=12000, help="Number of numbered HTML files")
    args, beam_args = parser.parse_known_args(argv)
    if args.expected_files < 1:
        parser.error("--expected-files must be positive")
    # Beam's local glob matcher expects native path separators on Windows.
    if "://" not in args.input:
        args.input = os.path.normpath(args.input)
    if "://" not in args.output:
        args.output = os.path.normpath(args.output)

    options = PipelineOptions(beam_args)
    FileSystems.set_options(options)
    for name in RESULT_NAMES:
        if FileSystems.exists(FileSystems.join(args.output, f"{name}.json")):
            parser.error(f"Results already exist under {args.output!r}. Choose a fresh output prefix.")

    runner = options.view_as(StandardOptions).runner or "DirectRunner"
    print(f"{runner} | Beam {beam.__version__} | Python {platform.python_version()}", flush=True)
    started = time.perf_counter()
    pipeline = beam.Pipeline(options=options)
    for name, rows in build_outputs(pipeline, args.input, args.expected_files).items():
        (
            rows
            | f"Encode {name}" >> beam.Map(json.dumps, sort_keys=True)
            | f"Write {name}" >> beam.io.WriteToText(
                FileSystems.join(args.output, name),
                file_name_suffix=".json",
                num_shards=1,
                shard_name_template="",
            )
        )
    result = pipeline.run()
    state = result.wait_until_finish()
    elapsed = time.perf_counter() - started
    if state != PipelineState.DONE:
        raise RuntimeError(f"Pipeline finished in state {state}")

    results = read_results(args.output)
    job_id = getattr(result, "job_id", None)
    if callable(job_id):
        print(f"Dataflow job ID {job_id()}")
    print(f"Wall time through successful completion {elapsed:.3f} seconds")
    summary = results["summary"]
    print(f"Processed {summary['files']:,} HTML files with {summary['outgoing_links']:,} links.")
    for name, field, title in (
        ("incoming", "page", "Incoming links"),
        ("outgoing", "page", "Outgoing links"),
        ("trigrams", "trigram", "Word trigrams"),
    ):
        print(f"\n{title}")
        for rank, row in enumerate(results[name], 1):
            print(f"{rank}. {row[field]}  {row['count']:,}")
    print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
