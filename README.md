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
  --mode triple-terms
```

Useful options:

```bash
--input-format turtle
--output-format turtle
--mode {triple-terms,reifying-triples,explicit-reifier,annotated-triple,annotated-triple-expanded}
--strict
--lenient
--assert-missing
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

### triple-terms

Does not assert `:s :p :o`.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

:r rdf:reifies <<( :s :p :o )>> .
:r :metadata :value .
```

### reifying-triples

Uses the compact reifying triple syntax when it is safe to omit the old reifier
identity. If the reifier is named or externally referenced, the converter falls
back to explicit reifier syntax to preserve semantics.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>

<< :s :p :o >> :metadata :value .
```

### explicit-reifier

Preserves the concrete classic reifier.

```turtle
VERSION "1.2"
PREFIX : <http://example.org/>

<< :s :p :o ~ :r >> :metadata :value .
```

### annotated-triple

Asserts `:s :p :o`. The converter requires that the base triple already exists in
the input graph, unless `--assert-missing` is passed.

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
