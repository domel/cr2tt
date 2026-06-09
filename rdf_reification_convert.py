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
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Identifier

RDF_REIFIES = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#reifies")

INPUT_FORMATS = {
    "turtle": "turtle",
    "ttl": "turtle",
    "n-triples": "nt",
    "ntriples": "nt",
    "nt": "nt",
    "xml": "xml",
    "rdfxml": "xml",
}

OUTPUT_FORMATS = {
    "turtle": "turtle",
    "ttl": "turtle",
}

MODES = {
    "triple-terms",
    "reifying-triples",
    "explicit-reifier",
    "annotated-triple",
    "annotated-triple-expanded",
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


class ConversionError(Exception):
    """Raised for invalid input patterns or unsafe mode choices."""

    pass


class TurtleWriter:
    """Minimal RDF 1.2 Turtle writer for the syntax this converter emits."""

    def __init__(self, graph: Graph) -> None:
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


def output_format(value: str | None, path: str | None) -> str:
    """Resolve the output format option.

    The converter currently writes only Turtle because the generated RDF 1.2
    constructs targeted by this tool are RDF 1.2 Turtle syntax.
    """

    if value and value != "auto":
        try:
            return OUTPUT_FORMATS[value.lower()]
        except KeyError:
            die(f"unknown output format: {value}")
    return format_from_path(path, OUTPUT_FORMATS) or "turtle"


def read_graph(args: argparse.Namespace) -> Graph:
    """Read RDF input from a file or stdin into an rdflib graph."""

    fmt = parse_format(args.input_format, args.input)
    if fmt is None:
        die("input format is required when reading from stdin or an unknown extension")

    graph = Graph()
    graph.bind("rdf", RDF)
    graph.bind("xsd", XSD)
    if args.input == "-":
        graph.parse(data=sys.stdin.read(), format=fmt, publicID=args.base)
    else:
        graph.parse(args.input, format=fmt, publicID=args.base)
    return graph


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

    subject_usage: dict[Identifier, list[tuple[Identifier, URIRef, Identifier]]] = defaultdict(list)
    object_usage: dict[Identifier, list[tuple[Identifier, URIRef, Identifier]]] = defaultdict(list)
    for triple in graph:
        subject, predicate, obj = triple
        subject_usage[subject].append(triple)
        object_usage[obj].append(triple)

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
            for triple in subject_usage[reifier]
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
            for triple in object_usage[cluster.reifier]
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
    """Build a triple term, replacing nested classic reifiers when available."""

    return TripleTerm(
        subject=triple_terms.get(cluster.subject, cluster.subject),
        predicate=cluster.predicate,
        object=triple_terms.get(cluster.object, cluster.object),
    )


def validate_cluster(cluster: ClassicCluster, config: Config) -> str | None:
    """Return a mode-specific validation error, or None when conversion is safe."""

    assertive_mode = config.mode in {"annotated-triple", "annotated-triple-expanded"}
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

    graph.bind("rdf", RDF)
    graph.bind("xsd", XSD)

    clusters, invalid_reifiers = extract_clusters(graph, config.strict)
    converted_reifiers: set[Identifier] = set()
    consumed: set[tuple[Identifier, URIRef, Identifier]] = set()
    lines: list[str] = []
    writer = TurtleWriter(graph)
    triple_terms: dict[Identifier, TripleTerm] = {}

    for cluster in clusters:
        reason = validate_cluster(cluster, config)
        if reason:
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
            if lines:
                lines.append("")
            lines.extend(generated)

        converted_reifiers.add(cluster.reifier)
        consumed.update(consumed_metadata)
        if consumed_base:
            consumed.add(cluster.raw_triple)
        if not config.keep_classic:
            consumed.update(cluster.technical)

    # Emit all input triples that were not consumed by a successful cluster
    # conversion. This preserves ordinary RDF, invalid lenient-mode clusters, and
    # external reifier references.
    for triple in sorted(graph, key=triple_sort_key):
        subject, predicate, obj = triple
        if triple in consumed:
            continue
        if (
            subject in converted_reifiers
            and is_technical_triple(triple)
            and not config.keep_classic
        ):
            continue
        if subject in invalid_reifiers:
            lines.append(f"{writer.term(subject)} {writer.term(predicate)} {writer.term(obj)} .")
            continue
        lines.append(f"{writer.term(subject)} {writer.term(predicate)} {writer.term(obj)} .")

    prefix_lines = writer.prefix_lines()
    body = "\n".join(lines).rstrip()
    if body:
        return "\n".join(prefix_lines) + "\n\n" + body + "\n"
    return "\n".join(prefix_lines) + "\n"


def conversion_lines(
    writer: TurtleWriter,
    cluster: ClassicCluster,
    triple_value: TripleTerm,
    config: Config,
) -> tuple[list[str], set[tuple[Identifier, URIRef, Identifier]], bool]:
    """Create Turtle lines for one cluster and report which input triples were consumed."""

    metadata = set(cluster.metadata)
    consumed_base = False

    if config.mode == "triple-terms":
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

    if config.mode == "reifying-triples":
        if can_use_implicit_reifier(cluster):
            return (
                [
                    (
                        f"{writer.reifying_triple(triple_value)} "
                        f"{writer.predicate_object_list(cluster.metadata)}"
                    )
                ],
                metadata,
                False,
            )
        warn(f"{cluster.reifier}: using explicit reifier syntax to preserve the reifier identity")
        return explicit_reifier_lines(writer, cluster, triple_value, metadata)

    if config.mode == "explicit-reifier":
        return explicit_reifier_lines(writer, cluster, triple_value, metadata)

    if config.mode == "annotated-triple":
        consumed_base = cluster.asserted
        line = (
            f"{writer.term(triple_value.subject)} {writer.term(triple_value.predicate)} "
            f"{writer.term(triple_value.object)} ~ {writer.term(cluster.reifier)}"
            f"{writer.annotation_block(cluster.metadata)} ."
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
        )
        return [base_line, *expanded], consumed_metadata, consumed_base

    raise ConversionError(f"unsupported mode: {config.mode}")


def explicit_reifier_lines(
    writer: TurtleWriter,
    cluster: ClassicCluster,
    triple_value: TripleTerm,
    metadata: set[tuple[Identifier, URIRef, Identifier]],
) -> tuple[list[str], set[tuple[Identifier, URIRef, Identifier]], bool]:
    """Serialize a cluster using explicit reifier syntax or rdf:reifies fallback."""

    if metadata:
        return (
            [
                (
                    f"{writer.reifying_triple(triple_value, cluster.reifier)} "
                    f"{writer.predicate_object_list(cluster.metadata)}"
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
    mode: str = "triple-terms",
    strict: bool = True,
    assert_missing: bool = False,
    keep_classic: bool = False,
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
            mode=mode,
            strict=strict,
            assert_missing=assert_missing,
            keep_classic=keep_classic,
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
        help="output format (RDF 1.2 Turtle is currently supported)",
    )
    parser.add_argument(
        "-m",
        "--mode",
        default="triple-terms",
        choices=sorted(MODES),
        help="RDF 1.2 output mode",
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
    parser.add_argument("--base", help="base IRI used while parsing")
    parser.add_argument("--base-iri", dest="base", help=argparse.SUPPRESS)
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    """Normalize CLI aliases and reject unsupported output choices."""

    args.input = args.input_option or args.input_path
    if args.input is None:
        die("input file is required")
    if output_format(args.output_format, args.output) != "turtle":
        die("only Turtle output is supported for RDF 1.2 reification syntax")
    return args


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    args = normalize_args(build_parser().parse_args(argv))
    try:
        graph = read_graph(args)
        text = convert_graph(
            graph,
            Config(
                mode=args.mode,
                strict=args.strict,
                assert_missing=args.assert_missing,
                keep_classic=args.keep_classic,
            ),
        )
        write_text_output(text, args.output)
    except ConversionError as exc:
        die(str(exc))
    except BrokenPipeError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
