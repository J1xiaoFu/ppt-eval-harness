# Demo cases

The tracked `examples/demo` directory contains one portable editable PPTX, a source text file,
and four case manifests. Manifest paths beginning with `./` are resolved relative to the JSON
file, so the cases work after cloning the repository into any directory.

Generate a fresh copy without modifying the tracked examples:

```powershell
python examples/generate_demo.py
```

The default output is the ignored `var/demo-generated` directory. Choose another location with:

```powershell
python examples/generate_demo.py --output-dir var/demo-custom
```

The same deck intentionally exercises full evaluation, baseline-only evaluation, and degraded
scenario paths.
