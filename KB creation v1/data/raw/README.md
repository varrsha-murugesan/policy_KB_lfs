# data/raw — licensed UMLS files (git-ignored)

`python scripts/download_umls_data.py` downloads the current **UMLS Metathesaurus Full Subset**
zip here and extracts only the files this project needs into `data/raw/META/`:

| file | required | used for |
|---|---|---|
| MRCONSO.RRF | yes | atoms: codes, strings, CUIs, term types, suppression |
| MRSTY.RRF | yes | semantic types (procedure lexicon selection) |
| MRREL.RRF | yes | concept relationships (hierarchy, possibly-synonymous) |
| MRSAB.RRF | yes | vocabulary versions and restriction levels |
| MRDEF.RRF | optional | definitions |
| MRRANK.RRF | optional | preferred-name ranking |
| MRSAT.RRF | optional (`--with-mrsat`) | CPT full descriptions (very large) |

Manual alternative: download the zip from https://www.nlm.nih.gov/research/umls/licensedcontent/umlsknowledgesources.html
(UTS login, active UMLS license required) and run
`python scripts/download_umls_data.py --zip <path-to-zip>`.

Do **not** use the "Level 0 subset": it excludes CPT (restriction category 3).
These files are licensed content. Do not commit, email or upload them.
