# Location reference data

This folder contains static New York State location reference CSVs used by the Apprenticeship Matcher.

The app loads these files into the local SQLite database on startup. They are not pulled live from data.ny.gov at runtime.

## Files

### Labor_Market_Regions.csv

Source:

https://data.ny.gov/Economic-Development/Labor-Market-Regions/imem-myat/about_data

Used to map New York State counties to New York State Department of Labor labor market regions.

Expected columns:

```csv
Region,County
```

### New_York_State_Locality_Hierarchy_with_Websites.csv

Source:

https://data.ny.gov/Government-Finance/New-York-State-Locality-Hierarchy-with-Websites/55k6-h6qq/about_data

Used to map New York State cities, towns, villages, and borough/county records to counties.

Expected columns include:

```csv
SWIS Code,Type Code,Type,County Name,City Name,Town Name,Village Name,2nd County,Website,Municipality,GNIS ID,State FIPS,County Code,County FIPS
```

### location_aliases.csv

Small app-owned list of common location phrases that are not reliably covered by the official locality hierarchy, such as "NYC", "five boroughs", and neighborhood or hamlet names that appear in apprenticeship postings.

## Refreshing the data

Download fresh CSVs from the source pages above.

Rename them to:

- `Labor_Market_Regions.csv`
- `New_York_State_Locality_Hierarchy_with_Websites.csv`

Replace the files in this folder.
Restart the app so `data/_database.db` is rebuilt.
Run tests: `uv run pytest`