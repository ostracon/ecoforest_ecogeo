from datetime import timedelta

DOMAIN = "ecoforest_ecogeo"
MANUFACTURER = "Ecoforest"

# 1 minute default; sometimes needs a gap between data reads
POLLING_INTERVAL = timedelta(seconds=60)
