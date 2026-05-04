import pandas as pd
import numpy as np
import os

# Parameters
# Automatically get the directory where the script is located
base_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
output_file = os.path.join(base_dir, "dhw_profiles.xlsx")

# Domestic hot water temperature in Celsius (Change this if needed)
T_dhw = 60.0 

# Specific heat capacity of water in kJ/(kg*K)
cp_water = 4.186 

print("1. Creating the 't_h' column...")
# 1. Create t_h column (hours, 0.25 timestep, 35040 samples = 1 year)
t_h = np.arange(0, 35040 * 0.25, 0.25)
df_dhw_liters = pd.DataFrame({'t_h': t_h})

print("2. Reading DHW timeseries datasets...")
# 2. Add timeseries datasets from folders
profiles = []
for p in range(1, 6):
    for i in range(1, 21):
        profiles.append(f"{p}p_DHW_{i}")

for profile in profiles:
    p_group = f"{profile.split('_')[0]}_DHW" # e.g. '1p_DHW'
    file_path = os.path.join(base_dir, p_group, profile, f"{profile}_DHW.txt")
    if os.path.exists(file_path):
        # Read the file. It's a single column of data.
        data = pd.read_csv(file_path, header=None, names=[profile])
        
        # Ensure it has exactly 35040 rows, fill or truncate if necessary
        profile_data = data[profile].values
        if len(profile_data) > 35040:
            profile_data = profile_data[:35040]
        elif len(profile_data) < 35040:
            profile_data = np.pad(profile_data, (0, 35040 - len(profile_data)), 'constant', constant_values=0)
        parts = profile.split('_')
        new_col_name = f"DHW{parts[2]}_{parts[0]}"
        df_dhw_liters[new_col_name] = profile_data
    else:
        print(f"Warning: File not found {file_path}")

print("3. Reading bodemtemps_260.txt...")
# 3. Read bodemtemps_260.txt
bodem_file = os.path.join(base_dir, "bodemtemps_260.txt")

# Columns defined in the KNMI file format
bodem_cols = ['STN', 'YYYYMMDD', 'HH', 'TB1', 'TB2', 'TB3', 'TB4', 'TB5', 'TNB1', 'TNB2', 'TXB1', 'TXB2', 'empty']

# Read data, skip the first 17 lines, keeping data from line 18 onwards
# KNMI data sometimes has trailing commas and spaces, so skipinitialspace handles the formatting
df_bodem = pd.read_csv(bodem_file, skiprows=17, header=None, names=bodem_cols, skipinitialspace=True)

# Keep relevant columns only
df_bodem = df_bodem[['YYYYMMDD', 'HH', 'TB1', 'TB2', 'TB3', 'TB4', 'TB5']].copy()

print("4. Calculating ground temperatures for each 15-min timestep...")
# Convert to a datetime so we can interpolate
# We will use the most complete standard year (1981) from the data to map to our 1-year timeseries.
df_bodem['YYYYMMDD'] = df_bodem['YYYYMMDD'].astype(str)
year_to_use = df_bodem['YYYYMMDD'].str[:4].unique()[0] # e.g. 1981
df_bodem_year = df_bodem[df_bodem['YYYYMMDD'].str.startswith(year_to_use)].copy()

# The 6-hourly data has HH=6,12,18,24. In pandas/datetime, HH=24 is 00:00 of the next day.
df_bodem_year['datetime'] = pd.to_datetime(df_bodem_year['YYYYMMDD'], format='%Y%m%d') + pd.to_timedelta(pd.to_numeric(df_bodem_year['HH']), unit='h')
df_bodem_year.set_index('datetime', inplace=True)

# Convert temperatures from 0.1 degree Celsius to degree Celsius
for col in ['TB1', 'TB2', 'TB3', 'TB4', 'TB5']:
    df_bodem_year[col] = pd.to_numeric(df_bodem_year[col], errors='coerce') / 10.0

# Create a full year 15-minute index
# 35040 intervals of 15 min covers exactly 365 days
datetime_index = pd.date_range(start=f'{year_to_use}-01-01 00:00:00', periods=35040, freq='15min')

# Reindex and interpolate using TB5 (100 cm depth) as the default ground temperature
# (You can change this to TB3 or TB4 if you want shallower ground temps)
ground_temp = df_bodem_year['TB5'].reindex(datetime_index).interpolate(method='linear')
ground_temp = ground_temp.bfill().ffill() # Fill any potential edges (like the very first timesteps before 06:00)

print("5. Converting liters to kWh...")
# Calculate energy in kWh
# E (kWh) = m (kg) * cp (kJ/kgK) * dT (K) / 3600 (s)
# Note: 1 liter of water ~ 1 kg

df_dhw_kwh = df_dhw_liters.copy()
t_ground_array = ground_temp.values

for col in df_dhw_kwh.columns:
    if col != 't_h':
        # Volume is in liters (kg), dT is (T_dhw - T_ground)
        df_dhw_kwh[col] = df_dhw_liters[col] * cp_water * (T_dhw - t_ground_array) / 3600.0

print(f"6. Saving to Excel file: {output_file}")
# Use xlsxwriter or openpyxl engine
with pd.ExcelWriter(output_file) as writer:
    # First sheet: Liters
    df_dhw_liters.to_excel(writer, sheet_name='dhw_profiles_literquarterhour', index=False)
    
    # Second sheet: Bodem temps
    df_bodem.to_excel(writer, sheet_name='bodemtemps', index=False)
    
    # Third sheet: kWh
    df_dhw_kwh.to_excel(writer, sheet_name='dhw_profiles_kwh', index=False)

print("Process completed successfully!")
