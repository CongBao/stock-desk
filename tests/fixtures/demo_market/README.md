# Stock Desk public synthetic demo fixture

This directory contains deterministic, entirely synthetic test data. It is not
derived from an exchange, vendor, issuer, news publisher, or individual. The
fixture is dedicated to the public domain under CC0-1.0 and must never be used
as an investment recommendation.

`manifest.json` is the canonical `stock-desk-public-demo-v1` input. The seed
script derives content-addressed market datasets through the same repositories
and lake adapters used by the application. A declared `missing` category is an
intentional outcome and must not be replaced with a different kind of data.
