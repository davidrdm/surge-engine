# Connector fixtures

Recorded/synthesised API responses used to test the connectors offline. No live
network in CI.

Provenance of each file matters, because the trust level differs:

| File | Provenance |
|---|---|
| `priceline_com2_cars*.json` | **Real captured responses** from the CURRENT wrapper (`priceline-com2`), trimmed from the live two-window captures below. These are what `dry_run` serves and what the parsing contract is pinned against. A synthetic `detailsKey` is put back after redaction so the storage-stripping test stays meaningful on this shape. |
| `priceline_cars*.json` | **Real captured response from the SUPERSEDED wrapper** (`priceline8`), supplied by the operator. Kept deliberately: the connector accepts both envelopes so a pre-8.5 payload still parses, and these fixtures are what proves it. Note the seven `vehicles[]` rows carry only four vendor ids — the same car once per rate plan, which is the duplication 8.6 found the counting model had been reading as inventory. Session-bearing fields are retained so the stripping test is meaningful. |
| `fr24_*.json` | Synthesised from FR24's OpenAPI 3.1 specification, field-for-field. `FlightPositionsFull` has exactly 22 fields and no `category`; `FlightSummaryFull` has 26 including `category`. |
| `apidirect_*.json` | Synthesised from the published per-endpoint documentation. Note the deliberate shape difference: twitter uses `date`/`author`, news uses `published_datetime_utc`/`authors[]`. |
| `staying_*.json` | Synthesised from `https://api.stayingapi.com/openapi.json`. |
| `live/pricelinecom2_cars_{near,base}.json` | **Real captured responses** (8.5), two date windows on PHX from `priceline-com2`. These are the evidence for the wrapper swap: 510/758 vehicles with `peopleCapacity` and `counterType` intact, and 311 `id` values shared across windows. |
| `live/globalrentalcars_pricetrail_{near,base}.json` | **Real captured responses** from the runner-up, identical upstream data. Kept so the comparison can be re-checked without re-spending calls. |
| `live/bookingcom18_cars_{near,base}.json` | **Real captured responses** from the alternative upstream (Booking.com/rentalcars), showing the 500-row cap and the richer `location_type` vocabulary. |

Anything synthesised is replaced by a scrubbed live capture during the Phase 2
live smoke step (`-m live`). Until then, these prove the parsing contract but
not that the provider actually behaves this way — see `test_connectors.py` for
which assertions depend on which.
