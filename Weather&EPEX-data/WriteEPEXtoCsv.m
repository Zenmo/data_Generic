% Process weather data 2023 de Bilt
clear;
% profiles=csvread("db_profiles.csv")(2:end,:);

t=0:8759;
dt=1;

load epex2024.mat

%solar_e_prod_normalized = ZonInstraling_kWpm2/max(ZonInstraling_kWpm2) * 0.8;

csvarray = [t', epex2024'*1000];
csvwrite("Epex24.csv",csvarray)

