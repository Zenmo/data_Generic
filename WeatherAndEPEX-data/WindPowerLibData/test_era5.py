import cdsapi
import os

client = cdsapi.Client()

# Test with a known good year and minimal request
dataset = "reanalysis-era5-single-levels"
request = {
    "product_type": ["reanalysis"],
    "variable": ["2m_temperature"],
    "year": ["2024"],
    "month": ["01"],
    "day": ["01"],
    "time": ["00:00"],
    "data_format": "grib",
    # "download_format": "zip", # Testing without zip first
    "area": [52, 4, 51, 5] # Small box instead of point
}

try:
    print("Starting test retrieval...")
    client.retrieve(dataset, request).download("test_output.grib")
    print("Success!")
except Exception as e:
    print(f"Failed with error: {e}")
finally:
    if os.path.exists("test_output.grib"):
        os.remove("test_output.grib")
