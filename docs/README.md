# Documentation Diagrams

This directory contains PlantUML diagrams describing the architecture of
`rdf_reification_convert.py`.

## Files

- `RDFReificationConvertFlow.puml` - conversion flow from CLI input to RDF 1.2 Turtle output.
- `RDFReificationConvertStructure.puml` - main data structures and pipeline functions.
- `RDFReificationConvertModes.puml` - output-mode decision logic.

Generated PDF files:

- `RDFReificationConvertFlow.pdf`
- `RDFReificationConvertStructure.pdf`
- `RDFReificationConvertModes.pdf`

## Render

With PlantUML installed:

```bash
plantuml -tpdf docs/*.puml
```

With the PlantUML JAR:

```bash
java -jar /path/to/plantuml.jar -tpdf docs/*.puml
```

If direct PDF output is not available in the local PlantUML/JVM setup, render SVG
first and convert it to PDF:

```bash
java -jar /path/to/plantuml.jar -tsvg docs/*.puml
for svg in docs/*.svg; do
  rsvg-convert -f pdf -o "${svg%.svg}.pdf" "$svg"
done
```

Graphviz `dot` is required by PlantUML for class and component diagrams.
