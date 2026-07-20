# Benchmark Incident and Retraction Log

Official benchmark incidents are append-only. Corrections create signed invalidation records and new
benchmark versions; historical evidence is not silently rewritten.

## Pre-alpha observations

### 2026-07-18: Phoenix versus hve-core case study is not official evidence

Status: documented, excluded from official ranking.

Reasons:

- downstream games were nested under a small number of harness builds;
- one invalid run loaded hve-core with unresolved Windows symlink pointers;
- saved patch files did not match recorded in-memory diff hashes after newline conversion;
- execution was local and self-attested.

Disposition:

- retain as an experimental runner case study;
- display an explicit non-benchmark disclaimer;
- do not use counts to name a harness winner;
- replace mutable evidence with content-addressed trial bundles.

## Incident entry template

```markdown
### YYYY-MM-DD: title

Affected releases/trials:
Detection:
Impact:
Containment:
Root cause:
Corrective action:
Score action:
Independent review:
```
