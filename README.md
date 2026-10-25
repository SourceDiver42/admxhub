# ADMX Hub

There is no central place for Group Policy administrative templates (ADMX/ADML): every vendor
publishes them somewhere else, in a different format, without a feed. ADMX Hub is:

- **a catalog**, rebuilt daily by GitHub Actions and published on GitHub Pages: the latest version of
  each product's templates, the vendor download URL, SHA-256 checksums of the download and of every
  file, languages, a policy index and **what changed between versions** (policies added, removed or
  changed), plus an Atom feed of new releases;
- **`admxhub`**, a vendor-neutral command line tool (a single `admxhub.exe` on Windows) to check,
  download and install templates into a central store and keep them up to date.

ADMX Hub hosts **metadata only**. Downloads always come from the vendors.

## Using the CLI

Download `admxhub.exe` from the [releases](../../releases) (Windows, no Python needed), or
`pip install .` on any OS.

```text
admxhub sources                      known products
admxhub check [ids]                  latest versions
admxhub status                       installed products in the store and available updates
admxhub install edge chrome          install into the store (asks before writing; -y to skip)
admxhub update                       update every installed product with a newer version
admxhub policies edge --search tls   list/search a product's policies
admxhub download windows11 --out .   download and extract only
```

- **Store:** `--store PATH`, or `$ADMXHUB_STORE`. Default on a domain-joined Windows machine is
  `\\<domain>\SYSVOL\<domain>\Policies\PolicyDefinitions`, otherwise `C:\Windows\PolicyDefinitions`.
  Only ADML languages already present in the store are installed (`--lang`, `--all-languages`).
- **Catalog:** `--catalog URL` or `$ADMXHUB_CATALOG` uses the published catalog (with checksums);
  without it the CLI asks the vendors directly.
- `--json` on any command for scripting; `--dry-run` for install/update.
- Before writing, `install` warns when a template defines a namespace that another file in the store
  already defines (the usual cause of "namespace already defined" errors in the Group Policy editor).
- Extraction uses what the OS provides: `msiexec /a`, `expand.exe` and the vendor's own extraction
  switch on Windows; `msitools` and `cabextract` on Linux and macOS (`apt install msitools cabextract`,
  `brew install msitools cabextract`).

## How products are described

The tool knows no vendor. Each product is a YAML file in [`src/admxhub/sources/`](src/admxhub/sources)
(schema: [`schema/source.schema.json`](schema/source.schema.json)) using one of four generic
discovery methods:

| Type | For | Example |
|---|---|---|
| `static` | a fixed "latest" URL; version from HTTP headers or `version_from` a file inside | Chrome |
| `redirect` | a short link redirecting to a versioned file name | FSLogix |
| `json` | any JSON API, fields picked with [JMESPath](https://jmespath.org) | GitHub releases, Edge |
| `html` | a download page; the link is found by regex | Microsoft Download Center |

```yaml
discovery:
  type: json
  url: https://api.github.com/repos/mozilla/policy-templates/releases/latest
  headers:
    Authorization: Bearer ${GITHUB_TOKEN}     # only sent when the variable is set
  items: root.assets
  filter: {field: name, regex: '^policy_templates_v[\d.]+\.zip$'}
  fields: {version: root.tag_name, url: item.browser_download_url, sha256: item.digest}
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to add a product.

## The catalog

```
catalog/v1/index.json                    all products: latest version, files + checksums, health
catalog/v1/products/<id>.json            version history (up to 50 versions)
catalog/v1/policies/<id>/<version>.json  policy index: name, category, registry key/value, scope
catalog/v1/feed.xml                      Atom feed of new versions
```

Schema: [`schema/catalog-v1.schema.json`](schema/catalog-v1.schema.json). The `v1` path stays stable;
breaking changes get a new version.

## GitHub Actions

| Workflow | What it does |
|---|---|
| `ci.yml` | tests on Linux and Windows for every push/PR; non-blocking live check of all vendor sources |
| `catalog.yml` | daily (and on source changes): builds the catalog, keeps history on the `data` branch, deploys GitHub Pages, opens an issue when a source fails 3 runs in a row and closes it when it recovers |
| `windows-cli.yml` | builds `admxhub.exe` with PyInstaller on Windows, smoke-tests zip/cab/msi/exe extraction and installing; tags `v*` publish a release with the exe and its SHA-256 |

### Repository setup

1. **Settings → Pages → Source: GitHub Actions.**
2. **Custom domain (later):** add a DNS record, then set the repository variable `PAGES_DOMAIN`
   (Settings → Secrets and variables → Actions → Variables). The workflow writes the `CNAME` file.
3. **Release:** bump `__version__` in `src/admxhub/__init__.py`, tag `vX.Y.Z` and push the tag.

## Hosting template files

Not enabled. The build can copy templates of sources marked `mirror: true` into `mirror/` (see
[`mirror/README.md`](mirror/README.md)), but no source sets it, and the validator refuses it for
proprietary templates.

## License

Code: MIT ([LICENSE](LICENSE)). Catalog data: CC0 ([LICENSE-DATA](LICENSE-DATA)). Vendor research
credits: [NOTICE](NOTICE).
