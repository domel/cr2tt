# Classic RDF Reification to RDF 1.2 Turtle

`rdf_reification_convert.py` converts classic RDF 1.0/RDF 1.1 reification
patterns to RDF 1.2 Turtle forms.

Input pattern:

```turtle
@prefix : <http://example.org/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

:r a rdf:Statement ;
   rdf:subject :s ;
   rdf:predicate :p ;
   rdf:object :o ;
   :metadata :value .
```

The output always starts with:

```turtle
VERSION "1.2"
```

The RDF 1.2 Turtle syntax reference is:

https://www.w3.org/TR/rdf12-turtle/

## Usage

```bash
python3 rdf_reification_convert.py \
  --input input.ttl \
  --output output.ttl \
  --mode reified-triple-expanded
```

Useful options:

```bash
--input-format turtle
--output-format turtle
--mode {reified-triple-expanded,reified-triple,reified-triple-explicit,annotated-triple,annotated-triple-explicit,annotated-triple-expanded}
--base-triple-policy {preserve,require,forbid-extra-asserted}
--strict
--lenient
--assert-missing
--allow-asserting-conversion
--validate-only
--keep-statement-type
--keep-classic
--drop-classic
--base IRI
```

`--strict` is the default. Invalid or ambiguous classic reification patterns fail
the conversion. With `--lenient`, invalid patterns are left unchanged and a warning
is printed.

`--drop-classic` is the default. It removes technical classic reification triples
after conversion. `--keep-classic` keeps them beside the RDF 1.2 representation.

## Output Modes

Legacy mode names `triple-terms`, `reifying-triples`, and `explicit-reifier`
are still accepted as aliases.

### reified-triple-expanded

Does not assert `:s :p :o`.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

:r rdf:reifies <<( :s :p :o )>> .
:r :metadata :value .
```

### reified-triple

Uses the compact reifying triple syntax. It is valid only for a local blank node
reifier with metadata and no external references to the old reifier.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>

<< :s :p :o >> :metadata :value .
```

### reified-triple-explicit

Preserves the concrete classic reifier.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>

<< :s :p :o ~ :r >> :metadata :value .
```

### annotated-triple

Asserts `:s :p :o` and uses annotation syntax without an explicit reifier. It is
valid only for a local blank node reifier with metadata and no external
references to the old reifier.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>

:s :p :o {| :metadata :value |} .
```

### annotated-triple-explicit

Asserts `:s :p :o` and preserves the concrete classic reifier.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>

:s :p :o ~ :r {| :metadata :value |} .
```

### annotated-triple-expanded

Also asserts `:s :p :o`, with metadata written using a reifying triple.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>

:s :p :o .
<< :s :p :o ~ :r >> :metadata :value .
```

## Assertion Semantics

Classic RDF reification describes a triple but does not assert it. Triple terms
and `rdf:reifies` can also describe an unasserted triple. Annotation syntax is
different: it asserts the annotated triple. For that reason, annotation modes fail
on missing base triples unless the user explicitly chooses `--assert-missing`.

## Development

Run tests:

```bash
python3 -m pytest
```

Run linter and formatter:

```bash
python3 -m ruff check .
python3 -m ruff format .
```
