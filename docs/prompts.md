# Prompts used for the Geoapify slice

Short record of the instructions behind this change, and what a person had to correct. The flood method (Copernicus HAND and the JRC permanent-water mask) was not rewritten. Place names in the tests are fixtures, not live Geoapify answers.

## 1. Place string to a screened AOI (used as specified)

Add an OpenAPI-style REST client for Geoapify Geocoding and Places. `GEOAPIFY_API_KEY` comes from the environment. `--place "West Lake, Hangzhou"` geocodes, builds an AOI from the result, and runs the existing flood screening. Nearby places go into the bilingual memo and the GeoJSON. Tag a fetched Geoapify field **LIVE**. Do not invent places. Do not claim flood depth from Geoapify. `--demo` stays offline and does not need a key.

What held: `GET https://api.geoapify.com/v1/geocode/search` and `GET https://api.geoapify.com/v2/places`, stdlib HTTP, mocked tests.

## 2. Travel time was deferred, then required in this same PR (human fix)

The first instruction said a travel-time isoline could be a follow-up PR, and that geocode plus places had to ship first. A later checklist (acceptance B.3) required the isoline in this PR: reachable-area GeoJSON from a point, a time, and a mode. It also said not to open a second PR.

Fix: call `GET https://api.geoapify.com/v1/isoline` with `type=time` (range in seconds). `--place` requests a 10-minute `drive` isoline unless `--no-isoline` is set. The tool name is `travel_time_isoline`. If the API returns HTTP 202, the client fetches that `id` once. If the polygon is still absent, the report says **PENDING** and does not draw a circle.

## 3. A geocoder bbox is not always a screening AOI (human fix)

The task said to build a reasonable AOI from the geocoder bbox or a buffer. A city-scale or country-scale bbox can trip the existing 5° rejection, and a building bbox is too small to screen. Using every bbox unchanged would either crash or screen the wrong area.

Fix, chosen by hand and written into the report: use the geocoder bbox only when both sides are between 400 m and 8 km. Otherwise build a square equirectangular buffer around the returned point (default 2 km, `--aoi-buffer-m`). The memo states which rule was used. The square is not a flood extent. `--place "West Lake, Hangzhou"` therefore does not reproduce the hand-drawn sample AOI, and its MOCK scores are not the sample 45.1.

## 4. Demo key in the public tree (explicit authorization, not a guess)

Geoapify’s co-founder authorized publishing the Open Geospatial Program demo key. It is only in `.env.example`, with their note. Runtime still reads `GEOAPIFY_API_KEY`. `.env` stays gitignored. Tests do not call the live API. A missing key fails `--place` with an error and does not invent coordinates.
