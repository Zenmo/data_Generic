% Process weather data 2023 de Bilt
clear;
% profiles=csvread("db_profiles.csv")(2:end,:);

t=0:16079;
dt=1;

startrow=14;

%Weersdata=csvread("Weersdata2023deBilt.txt");
Weersdata=csvread("WeersdataRotterdam_jan23-Okt24.txt");

ZonInstraling_kWpm2 = Weersdata(startrow+t,6)*100*100/3600/1000;
max(ZonInstraling_kWpm2);
WindsnelheidH10_mps=Weersdata(startrow+t,4)*0.1;
ruwheidshoogte_m=0.05;
WindsnelheidH100_mps=WindsnelheidH10_mps*log(100/ruwheidshoogte_m)/log(10/ruwheidshoogte_m);
Temperatuur_degC=Weersdata(startrow+t,5)*0.1;

load WindTurbinePower.mat;
turbinepower_r = [0; 0; turbinepower_kW]/max(turbinepower_kW);
windspeed_mps = [ 0; 2.9; windspeed_mps];

windPower_r = interp1(windspeed_mps, turbinepower_r, min(WindsnelheidH100_mps,max(windspeed_mps)));
sum(windPower_r)

load epexJan23-Okt24.mat

solar_e_prod_normalized = ZonInstraling_kWpm2/max(ZonInstraling_kWpm2) * 0.8;
sum(solar_e_prod_normalized)

return

csvarray = [t', windPower_r, solar_e_prod_normalized, Temperatuur_degC, epex(1:end-1)'*1000];
csvwrite("WeatherEpexJan23Okt24.csv",csvarray)

return

%%
ZuidenwindOpwek=dlmread("Zuidenwind - Cooperwiek.csv");
ZuidenwindOpwek2022_kWh=ZuidenwindOpwek(2:35041,3);
ZuidenwindOpwek2023_kWh=ZuidenwindOpwek(2+35040:1+35040*2,3);
ZuidenwindOpwek2021_kWh=ZuidenwindOpwek(2+35040*2:end,3);

sum(ZuidenwindOpwek2021_kWh)/2352
sum(ZuidenwindOpwek2022_kWh)/2352
sum(ZuidenwindOpwek2023_kWh)/2352
