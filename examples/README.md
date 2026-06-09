# Examples

This directory contains representative inputs for `rdf_reification_convert.py`
and generated RDF 1.2 Turtle outputs.

Run commands from the repository root.

## Basic Conversion

Classic reification without an asserted base triple:

```bash
python3 rdf_reification_convert.py \
  --input examples/simple_classic.ttl \
  --output examples/output_simple_triple_terms.ttl \
  --mode triple-terms
```

The explicit reifier shorthand preserves the named reifier:

```bash
python3 rdf_reification_convert.py \
  --input examples/simple_classic.ttl \
  --output examples/output_simple_explicit_reifier.ttl \
  --mode explicit-reifier
```

## Local Blank Node Reifier

This case can safely use the implicit reifying triple syntax:

```bash
python3 rdf_reification_convert.py \
  --input examples/blank_reifier.ttl \
  --output examples/output_blank_reifying_triples.ttl \
  --mode reifying-triples
```

## Annotated Triples

Annotation modes assert the base triple. This input already contains the base
triple, so `--assert-missing` is not needed:

```bash
python3 rdf_reification_convert.py \
  --input examples/asserted_annotation.ttl \
  --output examples/output_asserted_annotated_triple.ttl \
  --mode annotated-triple
```

Expanded annotation form:

```bash
python3 rdf_reification_convert.py \
  --input examples/asserted_annotation.ttl \
  --output examples/output_asserted_annotated_triple_expanded.ttl \
  --mode annotated-triple-expanded
```

If the input does not contain the asserted base triple, annotation modes fail
unless the user explicitly allows assertion:

```bash
python3 rdf_reification_convert.py \
  --input examples/simple_classic.ttl \
  --mode annotated-triple
```

Intentional assertion of a missing base triple:

```bash
python3 rdf_reification_convert.py \
  --input examples/simple_classic.ttl \
  --output examples/output_simple_annotated_with_assert_missing.ttl \
  --mode annotated-triple \
  --assert-missing
```

## External Reference

When another triple references the old reifier, use `explicit-reifier`:

```bash
python3 rdf_reification_convert.py \
  --input examples/external_reference.ttl \
  --output examples/output_external_reference_explicit_reifier.ttl \
  --mode explicit-reifier
```

## Nested Reification

Nested classic reification is converted bottom-up:

```bash
python3 rdf_reification_convert.py \
  --input examples/nested_reification.ttl \
  --output examples/output_nested_explicit_reifier.ttl \
  --mode explicit-reifier
```

## Lenient Validation

Strict mode fails on incomplete classic reification:

```bash
python3 rdf_reification_convert.py \
  --input examples/invalid_incomplete.ttl \
  --mode triple-terms
```

Lenient mode leaves the invalid pattern unchanged:

```bash
python3 rdf_reification_convert.py \
  --input examples/invalid_incomplete.ttl \
  --output examples/output_invalid_lenient.ttl \
  --mode triple-terms \
  --lenient
```

## Quality Commands

```bash
python3 -m pytest
python3 -m ruff check .
python3 -m ruff format .
```
