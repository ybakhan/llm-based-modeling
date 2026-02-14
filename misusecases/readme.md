# PlantUML → SVG → PNG Conversion

Commands to convert PlantUML files to high-quality PNG images.

```bash
java -jar /path/to/plantuml.jar -tsvg atm.puml

inkscape "atm.svg" --export-type=png --export-dpi=600 --export-background=white
```
