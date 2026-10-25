# Contributing

## Adding a product

1. Create `src/admxhub/sources/<id>.yaml` (see `schema/source.schema.json` and the existing files).
2. Pick a discovery type: `static` (fixed URL), `redirect` (a link that redirects to a versioned file),
   `json` (an API; select fields with JMESPath) or `html` (find the link on a download page).
3. Test it:
   ```sh
   pip install -e ".[dev]"
   admxhub validate
   admxhub check --live <id>
   admxhub download <id> --out /tmp/t      # should list the extracted ADMX/ADML files
   ```
4. Open a pull request. CI validates the definition and checks it against the vendor.

Vendor URLs and quirks belong in the YAML file, never in the Python code.

## Development

```sh
pip install -e ".[dev]"
pytest                 # offline tests
pytest -m network      # live checks against all vendors (set GITHUB_TOKEN to avoid API rate limits)
```
