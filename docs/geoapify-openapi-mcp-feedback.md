# Geoapify OpenAPI and MCP feedback

Notes for a reviewer of geo-flood-agent 0.4.0. This file lists the Geoapify REST endpoints the screening client calls, how that relates to MCP, and the behaviours a person had to correct. Flood scores are unchanged: Copernicus HAND and the JRC water mask. Geoapify records are place context. They are not flood depth and they do not enter the susceptibility score.

How to run the same checks: [walkthrough.md](walkthrough.md). The instruction record for this slice: [prompts.md](prompts.md).

`GEOAPIFY_API_KEY` is read from the environment (or a gitignored `.env`). This file does not contain a key.

## Endpoints used

Screening uses the Python standard library (`urllib`) against the public REST APIs. The query parameter names match the OpenAPI 3.1 specs at <https://apidocs.geoapify.com/>. A screening run does not call the MCP server.

| Tool | Method and URL | Query names sent |
|------|----------------|------------------|
| `geocode_place` | `GET https://api.geoapify.com/v1/geocode/search` | `text`, `format` (`geojson`), `limit` (`1`), `lang` (`en`), `bias` (`countrycode:none`), `apiKey` |
| `search_nearby_places` | `GET https://api.geoapify.com/v2/places` | `categories`, `filter` (`circle:lon,lat,radiusMeters`), `bias` (`proximity:lon,lat`), `limit`, `apiKey` |
| `travel_time_isoline` | `GET https://api.geoapify.com/v1/isoline` | `lat`, `lon`, `type` (`time`), `mode`, `range` (seconds), `traffic` (`free_flow`), `apiKey` |

Reference docs: [forward geocoding](https://apidocs.geoapify.com/docs/geocoding/forward-geocoding/), [Places API](https://apidocs.geoapify.com/docs/places/), [Isoline API](https://apidocs.geoapify.com/docs/isolines/).

Names, coordinates, and polygons are copied from the JSON body. The client does not invent a place. An isoline polygon is a reachable area for that mode and duration. It is not a flood extent.

## MCP

Runtime screening stays on the REST calls above so the query names stay the OpenAPI names. MCP is for an AI client that should call Geoapify itself.

The client snippet is [`geoapify-mcp.json.example`](geoapify-mcp.json.example). Its URL is the MCP endpoint `https://api.geoapify.com/v1/mcp` ([MCP server](https://apidocs.geoapify.com/docs/mcp/)). The header is `x-api-key: ${env:GEOAPIFY_API_KEY}`. Do not paste a key into that JSON file. Geoapify also documents `https://api-eu.geoapify.com/v1/mcp`. This package’s REST client uses `https://api.geoapify.com`.

## What needed a human correction

1. **Isoline HTTP 202, one follow-up by id.** The first isoline response is not always a polygon. If the body has an id and no `Polygon` or `MultiPolygon`, and the status is HTTP 200 or 202, the client calls `GET /v1/isoline` once more with that `id`. There is no retry loop. If the status is still HTTP 202 and there is still no polygon, the report kind is **PENDING**. No circle is drawn in its place.

2. **Geocoder bbox versus a point buffer.** The geocoder bbox becomes the screening AOI only when both sides are between 400 m and 8 km. A missing bbox, a smaller bbox, or a larger bbox (city or country scale, which can also trip the 5° AOI guard) uses an equirectangular square around the returned point (`--aoi-buffer-m`, default 2000 m). The memo states which rule was used. The square is not a flood extent. `--place "West Lake, Hangzhou"` does not reproduce the hand-drawn sample AOI, so its MOCK composite is not 45.1.

3. **Do not invent places.** A failed Places call is **UNAVAILABLE** and lists no names. Features without coordinates are skipped and counted, not given a guessed point. A name is taken from `name`, then `address_line1`, then `formatted`, and is not translated or filled from another source. Geocoding with no feature builds no AOI.
