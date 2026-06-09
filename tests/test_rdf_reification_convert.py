from __future__ import annotations

from pathlib import Path

import pytest

from rdf_reification_convert import ConversionError, convert_text

FIXTURES = Path(__file__).parent / "golden"


def golden(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


SIMPLE_INPUT = """
@prefix : <http://example.org/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:r a rdf:Statement ;
   rdf:subject :s ;
   rdf:predicate :p ;
   rdf:object :o ;
   :source :dataset1 ;
   :confidence "0.9"^^xsd:decimal .
"""


def test_triple_terms_golden() -> None:
    assert convert_text(SIMPLE_INPUT, mode="triple-terms") == golden("triple_terms.ttl")


def test_explicit_reifier_preserves_object_references_golden() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :r a rdf:Statement ;
       rdf:subject :s ;
       rdf:predicate :p ;
       rdf:object :o ;
       :source :dataset1 .

    :bob :said :r .
    """
    assert convert_text(data, mode="explicit-reifier") == golden("explicit_reifier.ttl")


def test_reifying_triples_uses_implicit_reifier_for_local_blank_node() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    _:r a rdf:Statement ;
       rdf:subject :s ;
       rdf:predicate :p ;
       rdf:object :o ;
       :source :dataset1 .
    """
    output = convert_text(data, mode="reifying-triples")
    assert "<< :s :p :o >> :source :dataset1 ." in output
    assert "rdf:subject" not in output
    assert "_:r" not in output


def test_reifying_triples_falls_back_for_named_reifier() -> None:
    output = convert_text(SIMPLE_INPUT, mode="reifying-triples")
    assert "<< :s :p :o ~ :r >>" in output
    assert "rdf:subject" not in output


def test_annotated_triple_golden_when_base_triple_is_asserted() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :s :p :o .
    :r a rdf:Statement ;
       rdf:subject :s ;
       rdf:predicate :p ;
       rdf:object :o ;
       :source :dataset1 .
    """
    assert convert_text(data, mode="annotated-triple") == golden("annotated_triple.ttl")


def test_annotated_modes_reject_missing_assertion_without_flag() -> None:
    with pytest.raises(ConversionError, match="annotation modes assert"):
        convert_text(SIMPLE_INPUT, mode="annotated-triple")

    with pytest.raises(ConversionError, match="annotation modes assert"):
        convert_text(SIMPLE_INPUT, mode="annotated-triple-expanded")


def test_annotated_expanded_can_assert_missing_with_explicit_flag() -> None:
    output = convert_text(
        SIMPLE_INPUT,
        mode="annotated-triple-expanded",
        assert_missing=True,
    )
    assert ":s :p :o ." in output
    assert "<< :s :p :o ~ :r >> :confidence" in output


def test_incomplete_reification_is_error_in_strict_mode() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :r rdf:subject :s ;
       rdf:predicate :p .
    """
    with pytest.raises(ConversionError, match="expected one rdf:object"):
        convert_text(data, mode="triple-terms")


def test_incomplete_reification_is_left_unchanged_in_lenient_mode() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :r rdf:subject :s ;
       rdf:predicate :p .
    """
    output = convert_text(data, mode="triple-terms", strict=False)
    assert ":r rdf:predicate :p ." in output
    assert ":r rdf:subject :s ." in output


def test_multiple_component_values_are_errors() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :r rdf:subject :s1, :s2 ;
       rdf:predicate :p ;
       rdf:object :o .
    """
    with pytest.raises(ConversionError, match="expected one rdf:subject"):
        convert_text(data, mode="triple-terms")


def test_invalid_predicate_object_is_error() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :r rdf:subject :s ;
       rdf:predicate "not-an-iri" ;
       rdf:object :o .
    """
    with pytest.raises(ConversionError, match="rdf:predicate must be an IRI"):
        convert_text(data, mode="triple-terms")


def test_multiple_reifiers_for_same_triple_preserve_both_reifiers() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :r1 a rdf:Statement ; rdf:subject :s ; rdf:predicate :p ; rdf:object :o ; :m :v1 .
    :r2 a rdf:Statement ; rdf:subject :s ; rdf:predicate :p ; rdf:object :o ; :m :v2 .
    """
    output = convert_text(data, mode="reifying-triples")
    assert "<< :s :p :o ~ :r1 >> :m :v1 ." in output
    assert "<< :s :p :o ~ :r2 >> :m :v2 ." in output


def test_additional_reifier_types_are_preserved_as_metadata() -> None:
    data = """
    @prefix : <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    :r a rdf:Statement, :Evidence ;
       rdf:subject :s ;
       rdf:predicate :p ;
       rdf:object :o ;
       :source :dataset1 .
    """
    output = convert_text(data, mode="explicit-reifier")
    assert "rdf:type :Evidence" in output


def test_nested_reification_from_cr2tt_golden() -> None:
    data = """
    @prefix ex:  <http://example.org/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

    _:b1 a rdf:Statement ;
        rdf:subject ex:Bob ;
        rdf:predicate ex:knows ;
        rdf:object ex:Alice ;
        ex:certainty "0.9" .

    _:b2 a rdf:Statement ;
        rdf:subject ex:System ;
        rdf:predicate ex:validates ;
        rdf:object _:b1 ;
        ex:source "LogA" .
    """
    assert convert_text(data, mode="reifying-triples") == golden("nested_reifying_triples.ttl")


def test_rdf_generator_style_fixture_keeps_normal_triples() -> None:
    data = """
    @prefix ex: <http://example.org/data/> .
    @prefix foaf: <http://xmlns.com/foaf/0.1/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

    ex:alice a foaf:Person ; foaf:name "Alice" .
    ex:bob a foaf:Person ; foaf:name "Bob" .
    ex:alice foaf:knows ex:bob .

    ex:stmt1 a rdf:Statement ;
        rdf:subject ex:alice ;
        rdf:predicate foaf:knows ;
        rdf:object ex:bob ;
        ex:certainty "0.9"^^xsd:float .
    """
    output = convert_text(data, mode="triple-terms")
    assert "ex:stmt1 rdf:reifies <<( ex:alice foaf:knows ex:bob )>> ." in output
    assert "ex:alice foaf:knows ex:bob ." in output
    assert "rdf:subject" not in output
