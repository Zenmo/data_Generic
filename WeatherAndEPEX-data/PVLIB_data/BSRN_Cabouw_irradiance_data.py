# %%
import pvlib
# from pvlib.location import Location
# from pvlib.pvsystem import PVSystem
# from pvlib.modelchain import ModelChain
from pvlib import pvsystem, modelchain, location
import numpy as np
import pandas as pd
from IPython.display import display as show
import matplotlib.pyplot as plt

# %%
df, meta = pvlib.iotools.get_bsrn(
    station='CAB',  # three letter code for the Cabauw station
    start=pd.Timestamp(2023,1,1),
    end=pd.Timestamp(2023,12,31),
    username="bsrnftp",  # replace with your own username
    password="bsrn1",  # replace with your own password
)

# show(df.head(), scrollX=True, scrollCollapse=True, paging=False, maxColumns=100, dom="tpr")


site = location.Location(meta['latitude'], meta['longitude'], 'Europe/Amsterdam', name='Cabauw')
# site = Location.from_epw(meta, df)


# %%
weather = df[['ghi', 'dni', 'dhi']].copy()
# add temperature and wind if present, or set defaults
weather['temp_air'] = df['temp_air']
weather['wind_speed'] = 1  # m/s
# weather['wind_speed'] = df['wind_speed']
# if 'temp_air' in df:
#     weather['temp_air'] = df['temp_air']
# else:
#     weather['temp_air'] = 20  # °C

# if 'wind_speed' in df:
#     weather['wind_speed'] = df['wind_speed']
# else:
#     weather['wind_speed'] = 1  # m/s
# %%
## 35 degree tilt, south facing

# https://pvwatts.nlr.gov/pvwatts.php
# Default PVWatts losses parameters, in percentage:
losses_parameters = {
    'soiling': 2,
    'shading': 3,
    'snow': 0,
    'mismatch': 2,
    'wiring': 2,
    'connections': 0.5,
    'lid': 1.5,
    'nameplate_rating': 1,
    'age': 0,
    'availability': 3
}

# Old style PVSystem definition, for reference:
# system = pvsystem.PVSystem(
#    surface_tilt=35,          # tilt angle in degrees
#    surface_azimuth=180,      # facing south
#    module_parameters={       # example CEC module parameters
#        'pdc0': 1,          # rated DC power [W]
#        'gamma_pdc': -0.004,  # temperature coefficient [1/°C]
#    },
#    temperature_model_parameters=dict(a=-3.56, b=-0.075, deltaT=3),
#    inverter_parameters={     # inverter parameters
#        'pdc0': 1,          # DC input limit [W]
#        'pac0': 0.96,          # AC output power [W]
#        'eta_inv_nom': 0.95,  # nominal efficiency
#    },
#    racking_model='close_mount', # mounting configuration
#    module_type='glass_polymer', # module type    
#)

mount = pvsystem.FixedMount(
    surface_tilt=35,
    surface_azimuth=180,
    #racking_model='open_rack'
)

array = pvsystem.Array(
    mount=mount,
    #module_type='glass_polymer',
    module_parameters={'pdc0': 1.0, 'gamma_pdc': -0.004},
    temperature_model_parameters={'a': -3.56, 'b': -0.075, 'deltaT': 3},
)

system = pvsystem.PVSystem(
    arrays=[array],
    inverter_parameters={'pdc0': 1.0, 'pac0': 0.96, 'eta_inv_nom': 0.95},
)

# mc = ModelChain(system, site, aoi_model='physical', spectral_model='no_loss', temperature_model='sapm')
mc = modelchain.ModelChain(system, site, aoi_model='physical', spectral_model='no_loss', temperature_model='sapm', losses_model='pvwatts')

mc.run_model(weather)

# Plot PV production power:
plt.figure(figsize=(10, 6))
mc.results.ac.plot()

# RVO rekent voor een schaduwvrije zuidopstelling met ongeveer 340 kWh per 375 Wp per jaar = 907 kWh/kWp/jaar, dus 907 full load hours
# https://www.rvo.nl/sites/default/files/2022/01/Tool-zonnestroom-voor-maatschappelijk-vastgoed.pdf

print(f'Total full load hours for 35-deg south facing system: {np.sum(mc.results.ac[:60*24*366])/60} hours')
production_normalized_35degsouth = mc.results.ac[:60*24*366].resample('1h').mean()
print(f'Total full load hours for 35-deg south facing system after resampling: {np.sum(production_normalized_35degsouth[:24*366])} hours')
production_normalized_35degsouth.to_csv("data_PV_35degSouth_Cabouw.csv", header=True)

## 15 degree tilt, east-west facing
array_kwargs = dict(
    module_parameters=dict(pdc0=0.5, gamma_pdc=-0.004),
    temperature_model_parameters=dict(a=-3.56, b=-0.075, deltaT=3)
)

arrays = [
    pvsystem.Array(pvsystem.FixedMount(15, 270), name='West-Facing Array',
                   **array_kwargs),
    pvsystem.Array(pvsystem.FixedMount(15, 90), name='East-Facing Array',
                   **array_kwargs),
]

system = pvsystem.PVSystem(arrays=arrays, inverter_parameters=dict(pdc0=1, pac0=0.96, eta_inv_nom=0.95))

# mc = ModelChain(system, site, aoi_model='physical', spectral_model='no_loss', temperature_model='sapm')
mc = modelchain.ModelChain(system, site, aoi_model='physical', spectral_model='no_loss', temperature_model='sapm', losses_model='pvwatts')

mc.run_model(weather)

# Plot PV production power:
plt.figure(figsize=(10, 6))
mc.results.ac.plot()

# RVO rekent voor een schaduwvrije oostwest-opstelling 8% minder opbrengst dan een zuidopstelling, dus ongeveer 835 kWh/kWp/jaar, dus 835 full load hours
# https://www.rvo.nl/sites/default/files/2022/01/Tool-zonnestroom-voor-maatschappelijk-vastgoed.pdf

print(f'Total full load hours for 15-deg east-west facing system: {np.sum(mc.results.ac[:60*24*366])/60} hours')
production_normalized_15degeastwest = mc.results.ac[:60*24*366].resample('1h').mean()
print(f'Total full load hours for 15-deg east-west facing system after resampling: {np.sum(production_normalized_15degeastwest[:24*366])} hours')
production_normalized_15degeastwest.to_csv("data_PV_15degEastWest_Cabouw.csv", header=True)

# %%
