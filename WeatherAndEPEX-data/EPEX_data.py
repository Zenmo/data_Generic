import pandas as pd
from entsoe import EntsoePandasClient

API_TOKEN = "a7d69200-e71f-4bc7-9376-46311eb95f9a"
client = EntsoePandasClient(api_key=API_TOKEN)

start = pd.Timestamp("2025-01-01", tz="Europe/Amsterdam")
end   = pd.Timestamp("2026-01-01", tz="Europe/Amsterdam")

# query_day_ahead_prices returns a pandas Series with timestamps
prices = client.query_day_ahead_prices(country_code="NL", start=start, end=end)

# Make it a clean hourly table
df = prices.rename("price_eur_per_mwh").to_frame()
df.index = df.index.tz_convert("Europe/Amsterdam")
df = df.sort_index()

df.to_csv("NL_day_ahead_prices_2025_hourly.csv", index_label="time")
print(df.head(), df.tail())