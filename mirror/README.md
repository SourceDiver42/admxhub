# mirror/

Reserved for hosting copies of template files. **Nothing is hosted yet.**

When enabled (`admxhub build --mirror`), the catalog build copies the extracted ADMX/ADML files of
sources that set `mirror: true` to `mirror/<id>/<version>/` on the published site. The source
validator refuses `mirror: true` for templates with a `proprietary` license; only enable it for
licenses that explicitly allow redistribution (e.g. MPL-2.0, MIT) after checking the vendor's terms.
