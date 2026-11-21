# Export a searchable reading catalog

This optional helper creates a **local HTML title list** from an installed Lastbook library. It does not download or publish books and does not require network access or extra dependencies.

From the repository root:

```sh
python3 tools/export_reading_catalog.py --data /path/to/LastbookData --output reading-catalog.html
```

Open `reading-catalog.html` in a browser and use its search box to filter titles, collections, formats and publishers. The HTML file is self-contained; it contains no external resources.

The export includes catalog records with `kind` equal to `pdf`, `html`, or `pptx`. It excludes videos and internal metadata. The contents reflect **only the selected installed pack** and may differ between Small, Medium, and All. It does not verify whether an individual document can be redistributed, and the HTML is not a public hosted catalog or a per-title download endpoint.

Run the fixture tests with:

```sh
python3 -B -m unittest app.tests.test_export_reading_catalog
```

Related request: #270.
