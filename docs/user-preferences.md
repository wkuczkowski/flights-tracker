# User Travel Preferences

## Preferred origin cities

- Gdańsk
- Poznań
- Warszawa

These are city-level preferences. A specific airport is not implied. An explicit
origin in the user's request overrides this preference.

Use each provider-defined city airport group, without additionally including
nearby airports.

## Flight preferences

- Prefer direct connections when ranking and presenting options. Connections
  with stops remain eligible unless the current request explicitly requires
  direct travel.
- Use economy as the default cabin class. Do not fall back to another cabin
  class unless the current request allows it.

Explicit instructions in the current request override these preferences.
