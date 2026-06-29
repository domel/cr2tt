#!/usr/bin/env python3
"""
Convert classic RDF reification to RDF 1.2 Turtle reification forms.

The converter is self-contained. It parses RDF 1.0/RDF 1.1 classic reification
with rdflib, validates complete reification clusters, and writes the selected
RDF 1.2 Turtle representation manually because rdflib does not yet serialize
triple terms, reifying triples, or annotation syntax.
"""

from __future__ import annotations

import argparse
import csv
import io
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Identifier

RDF_REIFIES = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#reifies")

INPUT_FORMATS = {
    "turtle": "turtle",
    "ttl": "turtle",
    "n-triples": "nt",
    "ntriples": "nt",
    "nt": "nt",
    "trig": "trig",
    "n-quads": "nquads",
    "nquads": "nquads",
    "nq": "nquads",
    "xml": "xml",
    "rdfxml": "xml",
}

OUTPUT_FORMATS = {
    "turtle": "turtle",
    "ttl": "turtle",
    "trig": "trig",
}

DATASET_INPUT_FORMATS = {"trig", "nquads"}
DATASET_OUTPUT_FORMATS = {"trig"}

MODE_ALIASES = {
    "reified-triple-expanded": "reified-triple-expanded",
    "reified-triple": "reified-triple",
    "reified-triple-explicit": "reified-triple-explicit",
    "annotated-triple": "annotated-triple",
    "annotated-triple-explicit": "annotated-triple-explicit",
    "annotated-triple-expanded": "annotated-triple-expanded",
    "triple-terms": "reified-triple-expanded",
    "reifying-triples": "reifying-triples",
    "explicit-reifier": "reified-triple-explicit",
    "annotated-triple-implicit": "annotated-triple",
}

ENUM_MODE_ALIASES = {
    "REIFIED_TRIPLE_EXPANDED": "reified-triple-expanded",
    "REIFIED_TRIPLE": "reified-triple",
    "REIFIED_TRIPLE_EXPLICIT": "reified-triple-explicit",
    "ANNOTATED_TRIPLE": "annotated-triple",
    "ANNOTATED_TRIPLE_EXPLICIT": "annotated-triple-explicit",
    "ANNOTATED_TRIPLE_EXPANDED": "annotated-triple-expanded",
}

MODE_CHOICES = sorted({*MODE_ALIASES, *ENUM_MODE_ALIASES})

BASE_TRIPLE_POLICIES = {
    "preserve",
    "require",
    "forbid-extra-asserted",
}

ENUM_BASE_TRIPLE_POLICIES = {
    "PRESERVE": "preserve",
    "REQUIRE": "require",
    "FORBID_EXTRA_ASSERTED": "forbid-extra-asserted",
}

TECHNICAL_PREDICATES = {RDF.subject, RDF.predicate, RDF.object}


@dataclass(frozen=True)
class TripleTerm:
    """In-memory representation of an RDF 1.2 triple term.

    rdflib is used only for RDF 1.1-compatible input parsing here. RDF 1.2 triple
    terms are kept as a local object until `TurtleWriter` serializes them.
    """

    subject: object
    predicate: URIRef
    object: object


@dataclass(frozen=True)
class ClassicCluster:
    """A validated classic reification cluster.

    `metadata` contains non-technical triples where the reifier is the subject.
    External references where the reifier is the object are not moved into this
    structure; they remain ordinary graph triples and are emitted after converted
    clusters.
    """

    reifier: Identifier
    subject: Identifier
    predicate: URIRef
    object: Identifier
    metadata: tuple[tuple[Identifier, URIRef, Identifier], ...]
    technical: tuple[tuple[Identifier, URIRef, Identifier], ...]
    asserted: bool
    reification_count: int
    nontechnical_object_references: int

    @property
    def raw_triple(self) -> tuple[Identifier, URIRef, Identifier]:
        return (self.subject, self.predicate, self.object)


@dataclass(frozen=True)
class Config:
    """Conversion options shared by CLI and tests."""

    mode: str
    strict: bool
    assert_missing: bool
    keep_classic: bool
    base_triple_policy: str = "preserve"
    keep_statement_type: bool = False
    sort_output: bool = True


@dataclass
class ConversionStats:
    """Small execution counters used by validate-only and metrics output."""

    valid_clusters: int = 0
    invalid_clusters: int = 0
    converted_clusters: int = 0
    skipped_clusters: int = 0
    body_lines: int = 0

    def add(self, other: "ConversionStats") -> None:
        self.valid_clusters += other.valid_clusters
        self.invalid_clusters += other.invalid_clusters
        self.converted_clusters += other.converted_clusters
        self.skipped_clusters += other.skipped_clusters
        self.body_lines += other.body_lines


class ConversionError(Exception):
    """Raised for invalid input patterns or unsafe mode choices."""

    pass


class TurtleWriter:
    """Minimal RDF 1.2 Turtle writer for the syntax this converter emits."""

    def __init__(self, graph: Graph | Dataset) -> None:
        self.graph = graph
        self.used_prefixes: set[str] = set()

    def prefix_lines(self) -> list[str]:
        """Return VERSION and PREFIX directives used by serialized terms."""

        namespaces = {prefix: namespace for prefix, namespace in self.graph.namespaces()}
        lines = ['VERSION "1.2"']
        for prefix in sorted(self.used_prefixes):
            namespace = namespaces.get(prefix)
            if namespace is None:
                continue
            label = f"{prefix}:" if prefix else ":"
            lines.append(f"PREFIX {label} <{namespace}>")
        return lines

    def term(self, value: object) -> str:
        """Serialize one RDF term or local `TripleTerm` as Turtle."""

        if isinstance(value, TripleTerm):
            return self.triple_term(value)
        if isinstance(value, URIRef):
            return self.iri(value)
        if isinstance(value, BNode):
            return f"_:{value}"
        if isinstance(value, Literal):
            return self.literal(value)
        raise TypeError(f"unsupported RDF term: {value!r}")

    def iri(self, value: URIRef) -> str:
        """Serialize IRI using a known prefix when possible."""

        try:
            prefix, _namespace, local = self.graph.namespace_manager.compute_qname(
                str(value),
                generate=False,
            )
        except KeyError:
            return f"<{value}>"

        prefix = prefix or ""
        self.used_prefixes.add(prefix)
        if prefix:
            return f"{prefix}:{local}"
        return f":{local}"

    def literal(self, value: Literal) -> str:
        """Serialize a literal and remember its datatype prefix if needed."""

        if value.datatype is not None:
            self.iri(URIRef(value.datatype))
        return value.n3(self.graph.namespace_manager)

    def triple_term(self, value: TripleTerm) -> str:
        """Serialize RDF 1.2 triple term syntax: <<( s p o )>>."""

        return (
            f"<<( {self.term(value.subject)} {self.term(value.predicate)} "
            f"{self.term(value.object)} )>>"
        )

    def reifying_triple(self, value: TripleTerm, reifier: Identifier | None = None) -> str:
        """Serialize RDF 1.2 reifying triple syntax, optionally with `~ reifier`."""

        if reifier is None:
            return (
                f"<< {self.term(value.subject)} {self.term(value.predicate)} "
                f"{self.term(value.object)} >>"
            )
        return (
            f"<< {self.term(value.subject)} {self.term(value.predicate)} "
            f"{self.term(value.object)} ~ {self.term(reifier)} >>"
        )

    def predicate_object_list(
        self,
        triples: Iterable[tuple[Identifier, URIRef, Identifier]],
        indent: str = "  ",
    ) -> str:
        """Serialize metadata as a deterministic Turtle predicate-object list."""

        items = sorted(triples, key=triple_sort_key)
        parts = []
        for index, (_subject, predicate, obj) in enumerate(items):
            end = " ." if index == len(items) - 1 else " ;"
            prefix = "" if index == 0 else "\n" + indent
            parts.append(f"{prefix}{self.term(predicate)} {self.term(obj)}{end}")
        return "".join(parts)

    def annotation_block(self, triples: Iterable[tuple[Identifier, URIRef, Identifier]]) -> str:
        """Serialize metadata for RDF 1.2 annotation syntax."""

        items = sorted(triples, key=triple_sort_key)
        body = " ; ".join(f"{self.term(predicate)} {self.term(obj)}" for _, predicate, obj in items)
        return f" {{| {body} |}}" if body else ""


def warn(message: str) -> None:
    print(f"rdf-reification-convert: warning: {message}", file=sys.stderr)


def die(message: str) -> None:
    print(f"rdf-reification-convert: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def normalize_mode(value: str) -> str:
    """Resolve CLI mode names to an internal mode."""

    raw = value.strip()
    enum_key = raw.upper().replace("-", "_")
    if raw == raw.upper() and "_" in raw and enum_key in ENUM_MODE_ALIASES:
        return ENUM_MODE_ALIASES[enum_key]

    key = raw.lower().replace("_", "-")
    try:
        return MODE_ALIASES[key]
    except KeyError as exc:
        if enum_key in ENUM_MODE_ALIASES:
            return ENUM_MODE_ALIASES[enum_key]
        raise ConversionError(f"unsupported mode: {value}") from exc


def normalize_base_triple_policy(value: str) -> str:
    """Resolve CLI policy names to an internal policy."""

    enum_key = value.strip().upper().replace("-", "_")
    if enum_key in ENUM_BASE_TRIPLE_POLICIES:
        return ENUM_BASE_TRIPLE_POLICIES[enum_key]

    key = value.strip().lower().replace("_", "-")
    if key in BASE_TRIPLE_POLICIES:
        return key
    raise ConversionError(f"unsupported base triple policy: {value}")


def format_from_path(path: str | None, formats: dict[str, str]) -> str | None:
    """Infer an rdflib format name from a file extension."""

    if not path or path == "-":
        return None
    suffix = Path(path).suffix.lower().lstrip(".")
    return formats.get(suffix)


def parse_format(value: str | None, path: str | None) -> str | None:
    """Resolve the input format option, accepting `auto`."""

    if value and value != "auto":
        try:
            return INPUT_FORMATS[value.lower()]
        except KeyError:
            die(f"unknown input format: {value}")
    return format_from_path(path, INPUT_FORMATS)


def output_format(
    value: str | None,
    path: str | None,
    input_path: str | None = None,
) -> str:
    """Resolve the output format option.

    The converter writes RDF 1.2 Turtle for single graphs and RDF 1.2 TriG for
    datasets with named graphs.
    """

    if value and value != "auto":
        try:
            return OUTPUT_FORMATS[value.lower()]
        except KeyError:
            die(f"unknown output format: {value}")
    return (
        format_from_path(path, OUTPUT_FORMATS)
        or format_from_path(input_path, OUTPUT_FORMATS)
        or "turtle"
    )


def bind_standard_namespaces(rdf: Graph | Dataset) -> None:
    """Register namespaces used by generated RDF 1.2 constructs."""

    rdf.bind("rdf", RDF)
    rdf.bind("xsd", XSD)


def read_rdf(args: argparse.Namespace) -> Graph | Dataset:
    """Read RDF input from a file or stdin into an rdflib graph or dataset."""

    fmt = parse_format(args.input_format, args.input)
    if fmt is None:
        die("input format is required when reading from stdin or an unknown extension")

    rdf: Graph | Dataset = Dataset() if fmt in DATASET_INPUT_FORMATS else Graph()
    bind_standard_namespaces(rdf)
    if args.input == "-":
        rdf.parse(source=sys.stdin.buffer, format=fmt, publicID=args.base)
    else:
        rdf.parse(args.input, format=fmt, publicID=args.base)
    return rdf


def write_text_output(text: str, output: str) -> None:
    """Write converted Turtle text to a file or stdout."""

    if output == "-":
        sys.stdout.write(text)
        return
    Path(output).write_text(text, encoding="utf-8")


def term_sort_key(value: object) -> tuple[object, ...]:
    """Return a deterministic ordering key for RDF terms and triple terms."""

    if isinstance(value, TripleTerm):
        return (
            3,
            term_sort_key(value.subject),
            term_sort_key(value.predicate),
            term_sort_key(value.object),
        )
    if isinstance(value, URIRef):
        return (0, str(value))
    if isinstance(value, BNode):
        return (1, str(value))
    if isinstance(value, Literal):
        return (2, str(value), value.language or "", str(value.datatype or ""))
    return (9, repr(value))


def triple_sort_key(triple: tuple[object, object, object]) -> tuple[object, ...]:
    """Return a deterministic ordering key for triples."""

    return (term_sort_key(triple[0]), term_sort_key(triple[1]), term_sort_key(triple[2]))


def is_technical_triple(triple: tuple[Identifier, URIRef, Identifier]) -> bool:
    """Return true for triples that belong to classic RDF reification machinery."""

    subject, predicate, obj = triple
    return predicate in TECHNICAL_PREDICATES or (predicate == RDF.type and obj == RDF.Statement)


def extract_clusters(graph: Graph, strict: bool) -> tuple[list[ClassicCluster], set[Identifier]]:
    """Extract and validate classic reification clusters from the input graph.

    A cluster is accepted only when it has exactly one `rdf:subject`,
    `rdf:predicate`, and `rdf:object` value and those values are legal RDF triple
    components. Invalid clusters fail in strict mode or are returned to the caller
    as unchanged graph content in lenient mode. `rdf:type rdf:Statement` is
    treated as a technical triple when present, but it is not required for
    detection.
    """

    candidates: set[Identifier] = set()
    for predicate in TECHNICAL_PREDICATES:
        candidates.update(
            subject for subject, _predicate, _object in graph.triples((None, predicate, None))
        )

    valid: list[ClassicCluster] = []
    invalid: set[Identifier] = set()
    triple_to_reifiers: dict[tuple[Identifier, URIRef, Identifier], list[Identifier]] = defaultdict(
        list
    )

    for reifier in sorted(candidates, key=term_sort_key):
        subject_values = list(graph.objects(reifier, RDF.subject))
        predicate_values = list(graph.objects(reifier, RDF.predicate))
        object_values = list(graph.objects(reifier, RDF.object))

        errors: list[str] = []
        if len(subject_values) != 1:
            errors.append(f"expected one rdf:subject, found {len(subject_values)}")
        if len(predicate_values) != 1:
            errors.append(f"expected one rdf:predicate, found {len(predicate_values)}")
        if len(object_values) != 1:
            errors.append(f"expected one rdf:object, found {len(object_values)}")

        if errors:
            handle_invalid_cluster(reifier, errors, strict)
            invalid.add(reifier)
            continue

        subject = subject_values[0]
        predicate = predicate_values[0]
        obj = object_values[0]

        if isinstance(subject, Literal):
            errors.append("rdf:subject must be an IRI or blank node")
        if not isinstance(predicate, URIRef):
            errors.append("rdf:predicate must be an IRI")
        if not isinstance(obj, (URIRef, BNode, Literal)):
            errors.append("rdf:object must be an IRI, blank node, or literal")

        if errors:
            handle_invalid_cluster(reifier, errors, strict)
            invalid.add(reifier)
            continue

        typed_statement = (reifier, RDF.type, RDF.Statement)
        technical = [
            (reifier, RDF.subject, subject),
            (reifier, RDF.predicate, predicate),
            (reifier, RDF.object, obj),
        ]
        if typed_statement in graph:
            technical.append(typed_statement)

        metadata = tuple(
            triple
            for triple in graph.triples((reifier, None, None))
            if triple not in technical and not is_technical_triple(triple)
        )
        raw_triple = (subject, predicate, obj)
        triple_to_reifiers[raw_triple].append(reifier)

        valid.append(
            ClassicCluster(
                reifier=reifier,
                subject=subject,
                predicate=predicate,
                object=obj,
                metadata=tuple(sorted(metadata, key=triple_sort_key)),
                technical=tuple(sorted(technical, key=triple_sort_key)),
                asserted=raw_triple in graph,
                reification_count=0,
                nontechnical_object_references=0,
            )
        )

    counted = []
    valid_reifiers = {cluster.reifier for cluster in valid}
    for cluster in valid:
        # References from another valid cluster's rdf:object are handled by nested
        # triple-term substitution. Other object references must remain visible in
        # the output, so they prevent implicit anonymous reifier syntax.
        nontechnical_object_refs = sum(
            1
            for triple in graph.triples((None, None, cluster.reifier))
            if not (
                triple[1] == RDF.object
                and triple[0] in valid_reifiers
                and triple[0] != cluster.reifier
            )
        )
        counted.append(
            ClassicCluster(
                reifier=cluster.reifier,
                subject=cluster.subject,
                predicate=cluster.predicate,
                object=cluster.object,
                metadata=cluster.metadata,
                technical=cluster.technical,
                asserted=cluster.asserted,
                reification_count=len(triple_to_reifiers[cluster.raw_triple]),
                nontechnical_object_references=nontechnical_object_refs,
            )
        )

    return topological_clusters(counted, strict), invalid


def handle_invalid_cluster(reifier: Identifier, errors: list[str], strict: bool) -> None:
    """Fail or warn for one invalid classic reification candidate."""

    message = f"{reifier}: " + "; ".join(errors)
    if strict:
        raise ConversionError(message)
    warn(f"leaving invalid classic reification unchanged: {message}")


def topological_clusters(clusters: list[ClassicCluster], strict: bool) -> list[ClassicCluster]:
    """Order clusters bottom-up so nested reifiers can become triple terms."""

    by_reifier = {cluster.reifier: cluster for cluster in clusters}
    visiting: set[Identifier] = set()
    visited: set[Identifier] = set()
    ordered: list[ClassicCluster] = []
    cycle_nodes: set[Identifier] = set()

    def visit(cluster: ClassicCluster) -> None:
        if cluster.reifier in visited:
            return
        if cluster.reifier in visiting:
            cycle_nodes.update(visiting)
            return

        visiting.add(cluster.reifier)
        for dependency in (cluster.subject, cluster.object):
            nested = by_reifier.get(dependency)
            if nested is not None:
                visit(nested)
        visiting.remove(cluster.reifier)
        visited.add(cluster.reifier)
        ordered.append(cluster)

    for cluster in sorted(clusters, key=lambda item: term_sort_key(item.reifier)):
        visit(cluster)

    if cycle_nodes:
        labels = ", ".join(str(node) for node in sorted(cycle_nodes, key=term_sort_key))
        if strict:
            raise ConversionError(f"cyclic classic reification cannot be converted: {labels}")
        warn(f"leaving cyclic classic reification unchanged: {labels}")
        return [cluster for cluster in ordered if cluster.reifier not in cycle_nodes]

    return ordered


def cluster_triple_term(
    cluster: ClassicCluster,
    triple_terms: dict[Identifier, TripleTerm],
) -> TripleTerm:
    """Build a triple term, replacing nested object reifiers when available."""

    return TripleTerm(
        subject=cluster.subject,
        predicate=cluster.predicate,
        object=triple_terms.get(cluster.object, cluster.object),
    )


def validate_cluster(cluster: ClassicCluster, config: Config) -> str | None:
    """Return a mode-specific validation error, or None when conversion is safe."""

    if config.base_triple_policy == "require" and not cluster.asserted:
        return "Missing base triple"
    if config.base_triple_policy == "forbid-extra-asserted" and cluster.asserted:
        return "Triple already asserted"

    if config.mode in {"reified-triple", "annotated-triple"}:
        if cluster.reification_count > 1:
            return "Multiple reifications for same triple require explicit mode"
        if not can_use_implicit_reifier(cluster):
            return "Requires blank node, locality and metadata"

    if config.mode == "annotated-triple-explicit" and not cluster.metadata:
        return "Requires metadata declaration"

    assertive_mode = config.mode in {
        "annotated-triple",
        "annotated-triple-explicit",
        "annotated-triple-expanded",
    }
    if assertive_mode and not cluster.asserted and not config.assert_missing:
        return "annotation modes assert the base triple; use --assert-missing to allow this"

    if config.mode == "reifying-triples":
        if cluster.reification_count > 1:
            return None
        if not can_use_implicit_reifier(cluster):
            return None

    return None


def can_use_implicit_reifier(cluster: ClassicCluster) -> bool:
    """Check whether `<< s p o >> metadata` preserves the input semantics.

    The implicit form drops the concrete reifier identifier, so it is used only
    for local blank nodes that are not otherwise referenced.
    """

    return (
        isinstance(cluster.reifier, BNode)
        and cluster.reification_count == 1
        and cluster.nontechnical_object_references == 0
        and bool(cluster.metadata)
    )


def convert_graph(graph: Graph, config: Config) -> str:
    """Convert a parsed graph and return RDF 1.2 Turtle text."""

    buffer = io.StringIO()
    emit_converted_graph(graph, config, buffer)
    return buffer.getvalue()


def iter_graph_triples(
    graph: Graph,
    sort_output: bool,
) -> Iterable[tuple[Identifier, URIRef, Identifier]]:
    """Yield graph triples with optional deterministic sorting."""

    if sort_output:
        return sorted(graph, key=triple_sort_key)
    return graph


def write_body_line(output: TextIO, line: str) -> None:
    """Write one body line with a trailing newline."""

    output.write(line)
    output.write("\n")


def write_converted_body(
    graph: Graph,
    config: Config,
    writer: TurtleWriter,
    output: TextIO,
) -> ConversionStats:
    """Write converted graph body without prefixes and return counters."""

    bind_standard_namespaces(graph)

    clusters, invalid_reifiers = extract_clusters(graph, config.strict)
    stats = ConversionStats(valid_clusters=len(clusters), invalid_clusters=len(invalid_reifiers))
    converted_reifiers: set[Identifier] = set()
    consumed: set[tuple[Identifier, URIRef, Identifier]] = set()
    triple_terms: dict[Identifier, TripleTerm] = {}

    for cluster in clusters:
        reason = validate_cluster(cluster, config)
        if reason:
            stats.invalid_clusters += 1
            stats.skipped_clusters += 1
            if config.strict:
                raise ConversionError(f"{cluster.reifier}: {reason}")
            warn(f"leaving classic reification unchanged: {cluster.reifier}: {reason}")
            continue

        triple_value = cluster_triple_term(cluster, triple_terms)
        triple_terms[cluster.reifier] = triple_value

        generated, consumed_metadata, consumed_base = conversion_lines(
            writer, cluster, triple_value, config
        )
        if generated:
            if stats.body_lines:
                write_body_line(output, "")
                stats.body_lines += 1
            for line in generated:
                write_body_line(output, line)
                stats.body_lines += 1

        converted_reifiers.add(cluster.reifier)
        stats.converted_clusters += 1
        consumed.update(consumed_metadata)
        if consumed_base:
            consumed.add(cluster.raw_triple)
        if not config.keep_classic:
            consumed.update(cluster.technical)

    # Emit all input triples that were not consumed by a successful cluster
    # conversion. This preserves ordinary RDF, invalid lenient-mode clusters, and
    # external reifier references. In CLI mode this can iterate without sorting,
    # avoiding a full in-memory list of graph triples.
    for triple in iter_graph_triples(graph, config.sort_output):
        subject, predicate, obj = triple
        if triple in consumed:
            continue
        if (
            subject in converted_reifiers
            and is_technical_triple(triple)
            and not config.keep_classic
        ):
            continue
        write_body_line(
            output, f"{writer.term(subject)} {writer.term(predicate)} {writer.term(obj)} ."
        )
        stats.body_lines += 1

    return stats


def emit_converted_graph(graph: Graph, config: Config, output: TextIO) -> ConversionStats:
    """Write a converted graph with prefixes, buffering only the body on disk if needed."""

    writer = TurtleWriter(graph)
    with tempfile.SpooledTemporaryFile(
        mode="w+",
        encoding="utf-8",
        max_size=1024 * 1024,
    ) as body:
        stats = write_converted_body(graph, config, writer, body)
        output.write("\n".join(writer.prefix_lines()))
        if stats.body_lines:
            output.write("\n\n")
            body.seek(0)
            shutil.copyfileobj(body, output)
        else:
            output.write("\n")
    return stats


class IndentedWriter:
    """TextIO-like wrapper that indents non-empty lines as they are written."""

    def __init__(self, output: TextIO, indent: str) -> None:
        self.output = output
        self.indent = indent
        self.at_line_start = True

    def write(self, text: str) -> int:
        written = 0
        for char in text:
            if self.at_line_start and char != "\n":
                self.output.write(self.indent)
            self.output.write(char)
            self.at_line_start = char == "\n"
            written += 1
        return written


def dataset_named_graphs(dataset: Dataset) -> list[Graph]:
    """Return non-empty named graphs from an rdflib dataset."""

    default_id = dataset.default_graph.identifier
    return [
        graph for graph in dataset.graphs() if graph.identifier != default_id and len(graph) > 0
    ]


def has_named_graphs(dataset: Dataset) -> bool:
    """Return true when a dataset contains at least one non-empty named graph."""

    return bool(dataset_named_graphs(dataset))


def iter_dataset_graphs(dataset: Dataset, sort_output: bool) -> Iterable[Graph]:
    """Yield named graphs with optional deterministic ordering."""

    graphs = dataset_named_graphs(dataset)
    if sort_output:
        return sorted(graphs, key=lambda graph: term_sort_key(graph.identifier))
    return graphs


def emit_converted_dataset(dataset: Dataset, config: Config, output: TextIO) -> ConversionStats:
    """Write a converted dataset as RDF 1.2 TriG."""

    bind_standard_namespaces(dataset)
    writer = TurtleWriter(dataset)
    total_stats = ConversionStats()

    with tempfile.SpooledTemporaryFile(
        mode="w+",
        encoding="utf-8",
        max_size=1024 * 1024,
    ) as body:
        wrote_section = False
        default_graph = dataset.default_graph
        if len(default_graph) > 0:
            stats = write_converted_body(default_graph, config, writer, body)
            total_stats.add(stats)
            wrote_section = stats.body_lines > 0

        for graph in iter_dataset_graphs(dataset, config.sort_output):
            if wrote_section:
                write_body_line(body, "")
                total_stats.body_lines += 1
            body.write(f"{writer.term(graph.identifier)} {{\n")
            total_stats.body_lines += 1
            indented = IndentedWriter(body, "  ")
            stats = write_converted_body(graph, config, writer, indented)  # type: ignore[arg-type]
            total_stats.add(stats)
            body.write("}\n")
            total_stats.body_lines += 1
            wrote_section = True

        output.write("\n".join(writer.prefix_lines()))
        if total_stats.body_lines:
            output.write("\n\n")
            body.seek(0)
            shutil.copyfileobj(body, output)
        else:
            output.write("\n")

    return total_stats


def emit_converted_rdf(
    rdf: Graph | Dataset,
    config: Config,
    output_format_name: str,
    output: TextIO,
) -> ConversionStats:
    """Write graph or dataset output in the selected RDF 1.2 text format."""

    if isinstance(rdf, Dataset):
        if has_named_graphs(rdf):
            if output_format_name not in DATASET_OUTPUT_FORMATS:
                raise ConversionError("datasets with named graphs require TriG output")
            return emit_converted_dataset(rdf, config, output)
        return emit_converted_graph(rdf.default_graph, config, output)

    if output_format_name == "trig":
        return emit_converted_graph(rdf, config, output)
    return emit_converted_graph(rdf, config, output)


def validate_graph(graph: Graph, config: Config) -> ConversionStats:
    """Validate conversion without writing output."""

    clusters, invalid_reifiers = extract_clusters(graph, config.strict)
    stats = ConversionStats(valid_clusters=len(clusters), invalid_clusters=len(invalid_reifiers))
    for cluster in clusters:
        reason = validate_cluster(cluster, config)
        if reason:
            stats.invalid_clusters += 1
            stats.skipped_clusters += 1
            if config.strict:
                raise ConversionError(f"{cluster.reifier}: {reason}")
            warn(f"leaving classic reification unchanged: {cluster.reifier}: {reason}")
            continue
        stats.converted_clusters += 1
    return stats


def validate_rdf(rdf: Graph | Dataset, config: Config) -> ConversionStats:
    """Validate graph or dataset conversion without writing output."""

    if isinstance(rdf, Dataset):
        total = ConversionStats()
        if len(rdf.default_graph) > 0:
            total.add(validate_graph(rdf.default_graph, config))
        for graph in iter_dataset_graphs(rdf, config.sort_output):
            total.add(validate_graph(graph, config))
        return total
    return validate_graph(rdf, config)


def output_metadata(
    cluster: ClassicCluster,
    config: Config,
) -> tuple[tuple[Identifier, URIRef, Identifier], ...]:
    """Return metadata triples that should be attached to the RDF 1.2 reifier."""

    triples = list(cluster.metadata)
    typed_statement = (cluster.reifier, RDF.type, RDF.Statement)
    if config.keep_statement_type and (
        not config.keep_classic or typed_statement not in cluster.technical
    ):
        triples.append(typed_statement)
    return tuple(sorted(triples, key=triple_sort_key))


def conversion_lines(
    writer: TurtleWriter,
    cluster: ClassicCluster,
    triple_value: TripleTerm,
    config: Config,
) -> tuple[list[str], set[tuple[Identifier, URIRef, Identifier]], bool]:
    """Create Turtle lines for one cluster and report which input triples were consumed."""

    metadata = set(cluster.metadata)
    metadata_to_write = output_metadata(cluster, config)
    consumed_base = False

    if config.mode == "reified-triple-expanded":
        lines = [
            (
                f"{writer.term(cluster.reifier)} {writer.term(RDF_REIFIES)} "
                f"{writer.triple_term(triple_value)} ."
            )
        ]
        typed_statement = (cluster.reifier, RDF.type, RDF.Statement)
        if config.keep_statement_type and (
            not config.keep_classic or typed_statement not in cluster.technical
        ):
            lines.append(
                f"{writer.term(cluster.reifier)} {writer.term(RDF.type)} "
                f"{writer.term(RDF.Statement)} ."
            )
        return lines, set(), False

    if config.mode in {"reifying-triples", "reified-triple"}:
        if can_use_implicit_reifier(cluster):
            return (
                [
                    (
                        f"{writer.reifying_triple(triple_value)} "
                        f"{writer.predicate_object_list(metadata_to_write)}"
                    )
                ],
                metadata,
                False,
            )
        if config.mode == "reified-triple":
            raise ConversionError(f"{cluster.reifier}: Requires blank node, locality and metadata")
        warn(f"{cluster.reifier}: using explicit reifier syntax to preserve the reifier identity")
        return explicit_reifier_lines(writer, cluster, triple_value, metadata, metadata_to_write)

    if config.mode == "annotated-triple":
        consumed_base = cluster.asserted
        line = (
            f"{writer.term(triple_value.subject)} {writer.term(triple_value.predicate)} "
            f"{writer.term(triple_value.object)}"
            f"{writer.annotation_block(metadata_to_write)} ."
        )
        return [line], metadata, consumed_base

    if config.mode == "reified-triple-explicit":
        return explicit_reifier_lines(writer, cluster, triple_value, metadata, metadata_to_write)

    if config.mode == "annotated-triple-explicit":
        consumed_base = cluster.asserted
        line = (
            f"{writer.term(triple_value.subject)} {writer.term(triple_value.predicate)} "
            f"{writer.term(triple_value.object)} ~ {writer.term(cluster.reifier)}"
            f"{writer.annotation_block(metadata_to_write)} ."
        )
        return [line], metadata, consumed_base

    if config.mode == "annotated-triple-expanded":
        consumed_base = cluster.asserted
        base_line = (
            f"{writer.term(triple_value.subject)} {writer.term(triple_value.predicate)} "
            f"{writer.term(triple_value.object)} ."
        )
        expanded, consumed_metadata, _consumed_base = explicit_reifier_lines(
            writer,
            cluster,
            triple_value,
            metadata,
            metadata_to_write,
        )
        return [base_line, *expanded], consumed_metadata, consumed_base

    raise ConversionError(f"unsupported mode: {config.mode}")


def explicit_reifier_lines(
    writer: TurtleWriter,
    cluster: ClassicCluster,
    triple_value: TripleTerm,
    metadata: set[tuple[Identifier, URIRef, Identifier]],
    metadata_to_write: Iterable[tuple[Identifier, URIRef, Identifier]],
) -> tuple[list[str], set[tuple[Identifier, URIRef, Identifier]], bool]:
    """Serialize a cluster using explicit reifier syntax or rdf:reifies fallback."""

    metadata_items = tuple(metadata_to_write)
    if metadata_items:
        return (
            [
                (
                    f"{writer.reifying_triple(triple_value, cluster.reifier)} "
                    f"{writer.predicate_object_list(metadata_items)}"
                )
            ],
            metadata,
            False,
        )
    return (
        [
            (
                f"{writer.term(cluster.reifier)} {writer.term(RDF_REIFIES)} "
                f"{writer.triple_term(triple_value)} ."
            )
        ],
        set(),
        False,
    )


def convert_text(
    data: str,
    *,
    input_format: str = "turtle",
    mode: str = "reified-triple-expanded",
    strict: bool = True,
    assert_missing: bool = False,
    keep_classic: bool = False,
    base_triple_policy: str = "preserve",
    keep_statement_type: bool = False,
    base: str | None = None,
) -> str:
    """Convert RDF text directly.

    This helper is used by tests and is also useful for embedding the converter
    from other Python code.
    """

    graph = Graph()
    graph.bind("rdf", RDF)
    graph.bind("xsd", XSD)
    graph.parse(data=data, format=INPUT_FORMATS[input_format], publicID=base)
    return convert_graph(
        graph,
        Config(
            mode=normalize_mode(mode),
            strict=strict,
            assert_missing=assert_missing,
            keep_classic=keep_classic,
            base_triple_policy=normalize_base_triple_policy(base_triple_policy),
            keep_statement_type=keep_statement_type,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(
        description="Convert classic RDF reification to RDF 1.2 Turtle syntax."
    )
    parser.add_argument("input_path", nargs="?", help="input file path, or '-' for stdin")
    parser.add_argument("--input", dest="input_option", help="input file path, or '-' for stdin")
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="output file path, or '-' for stdout (default: stdout)",
    )
    parser.add_argument(
        "-i",
        "--input-format",
        default="auto",
        choices=["auto", *INPUT_FORMATS.keys()],
        help="input format (default: infer from extension)",
    )
    parser.add_argument(
        "-f",
        "--output-format",
        default="auto",
        choices=["auto", *OUTPUT_FORMATS.keys()],
        help="output format (RDF 1.2 Turtle/TriG are currently supported)",
    )
    parser.add_argument(
        "-m",
        "--mode",
        default="reified-triple-expanded",
        choices=MODE_CHOICES,
        help="RDF 1.2 output mode",
    )
    parser.add_argument(
        "--base-triple-policy",
        default="preserve",
        choices=sorted({*BASE_TRIPLE_POLICIES, *ENUM_BASE_TRIPLE_POLICIES}),
        help="whether the reified base triple must or must not already be asserted",
    )

    strict_group = parser.add_mutually_exclusive_group()
    strict_group.add_argument(
        "--strict",
        dest="strict",
        action="store_true",
        default=True,
        help="fail on invalid or unsafe classic reification patterns (default)",
    )
    strict_group.add_argument(
        "--lenient",
        dest="strict",
        action="store_false",
        help="leave invalid or unsafe patterns unchanged and print warnings",
    )

    classic_group = parser.add_mutually_exclusive_group()
    classic_group.add_argument(
        "--keep-classic",
        dest="keep_classic",
        action="store_true",
        default=False,
        help="keep old rdf:subject/rdf:predicate/rdf:object triples",
    )
    classic_group.add_argument(
        "--drop-classic",
        dest="keep_classic",
        action="store_false",
        help="drop old technical classic reification triples after conversion (default)",
    )

    parser.add_argument(
        "--assert-missing",
        action="store_true",
        help="allow annotation modes to assert a missing base triple",
    )
    parser.add_argument(
        "--allow-asserting-conversion",
        dest="assert_missing",
        action="store_true",
        help="alias for --assert-missing",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the selected conversion without writing RDF output",
    )
    parser.add_argument(
        "--keep-statement-type",
        action="store_true",
        help="attach rdf:type rdf:Statement to the RDF 1.2 reifier representation",
    )
    parser.add_argument(
        "--sort-output",
        action="store_true",
        help="sort emitted graph triples for deterministic output; costs memory on large graphs",
    )
    parser.add_argument(
        "--metrics-csv",
        help="append timing and cluster counters to this CSV file",
    )
    parser.add_argument("--base", help="base IRI used while parsing")
    parser.add_argument("--base-iri", dest="base", help=argparse.SUPPRESS)
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    """Normalize CLI aliases and reject unsupported output choices."""

    args.input = args.input_option or args.input_path
    if args.input is None:
        die("input file is required")
    args.mode = normalize_mode(args.mode)
    args.base_triple_policy = normalize_base_triple_policy(args.base_triple_policy)
    args.resolved_output_format = output_format(args.output_format, args.output, args.input)
    return args


def append_metrics_csv(
    path: str,
    *,
    parse_ms: float,
    conversion_ms: float,
    total_ms: float,
    stats: ConversionStats,
) -> None:
    """Append one metrics row compatible with CLI batch runs."""

    metrics_path = Path(path)
    write_header = not metrics_path.exists()
    with metrics_path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        if write_header:
            writer.writerow(
                [
                    "Parse_ms",
                    "ConversionOrValidation_ms",
                    "Total_ms",
                    "ValidClusters",
                    "InvalidClusters",
                    "ConvertedClusters",
                    "SkippedClusters",
                    "BodyLines",
                ]
            )
        writer.writerow(
            [
                f"{parse_ms:.6f}",
                f"{conversion_ms:.6f}",
                f"{total_ms:.6f}",
                stats.valid_clusters,
                stats.invalid_clusters,
                stats.converted_clusters,
                stats.skipped_clusters,
                stats.body_lines,
            ]
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    args = normalize_args(build_parser().parse_args(argv))
    total_start = time.perf_counter()
    try:
        parse_start = time.perf_counter()
        rdf = read_rdf(args)
        parse_ms = (time.perf_counter() - parse_start) * 1000

        config = Config(
            mode=args.mode,
            strict=args.strict,
            assert_missing=args.assert_missing,
            keep_classic=args.keep_classic,
            base_triple_policy=args.base_triple_policy,
            keep_statement_type=args.keep_statement_type,
            sort_output=args.sort_output,
        )

        conversion_start = time.perf_counter()
        if args.validate_only:
            stats = validate_rdf(rdf, config)
        elif args.output == "-":
            stats = emit_converted_rdf(rdf, config, args.resolved_output_format, sys.stdout)
        else:
            with Path(args.output).open("w", encoding="utf-8") as stream:
                stats = emit_converted_rdf(rdf, config, args.resolved_output_format, stream)
        conversion_ms = (time.perf_counter() - conversion_start) * 1000
        total_ms = (time.perf_counter() - total_start) * 1000

        if args.metrics_csv:
            append_metrics_csv(
                args.metrics_csv,
                parse_ms=parse_ms,
                conversion_ms=conversion_ms,
                total_ms=total_ms,
                stats=stats,
            )

        if args.validate_only and stats.invalid_clusters:
            return 1
    except ConversionError as exc:
        die(str(exc))
    except BrokenPipeError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
