% Process weather data 2023 de Bilt
clear;
% profiles=csvread("db_profiles.csv")(2:end,:);

t=0:8759;
dt=1;

startrow=14;

%Weersdata=csvread("Weersdata2023deBilt.txt");
load WindTurbinePower.mat;
turbinepower_r = [0; 0; turbinepower_kW]/max(turbinepower_kW);
windspeed_mps = [ 0; 2.9; windspeed_mps];


WindHoekVanHolland=csvread("result_windsnelheden_Hoek_van_Holland.txt");
WindHerwijnen=csvread("result_windsnelheden_Herwijnen.txt");
WindEll=csvread("result_windsnelheden_Ell.txt");

WindsnelheidH10_hvh_mps=WindHoekVanHolland(startrow+t,4)*0.1*0.8;
ruwheidshoogte_m=0.000000001;
WindsnelheidH100_hvh_mps=WindsnelheidH10_hvh_mps*log(100/ruwheidshoogte_m)/log(10/ruwheidshoogte_m);
windPower_hoekVanHolland_r = interp1(windspeed_mps, turbinepower_r, min(WindsnelheidH100_hvh_mps,max(windspeed_mps)));
sum(windPower_hoekVanHolland_r)

WindsnelheidH10_herwijnen_mps=WindHerwijnen(startrow+t,4)*0.1;
ruwheidshoogte_m=0.01;
WindsnelheidH100_herwijnen_mps=WindsnelheidH10_herwijnen_mps*log(100/ruwheidshoogte_m)/log(10/ruwheidshoogte_m);
windPower_herwijnen_r = interp1(windspeed_mps, turbinepower_r, min(WindsnelheidH100_herwijnen_mps,max(windspeed_mps)));
sum(windPower_herwijnen_r)

WindsnelheidH10_ell_mps=WindEll(startrow+t,4)*0.1;
ruwheidshoogte_m=0.005;
WindsnelheidH100_ell_mps=WindsnelheidH10_ell_mps*log(100/ruwheidshoogte_m)/log(10/ruwheidshoogte_m);
windPower_ell_r = interp1(windspeed_mps, turbinepower_r, min(WindsnelheidH100_ell_mps,max(windspeed_mps)));
sum(windPower_ell_r)

csvarray = [t', windPower_hoekVanHolland_r, windPower_herwijnen_r, windPower_ell_r];
csvwrite("WindPowers_L4L.csv",csvarray)

return

%%
ZuidenwindOpwek=dlmread("Zuidenwind - Cooperwiek.csv");
ZuidenwindOpwek2022_kWh=ZuidenwindOpwek(2:35041,3);
ZuidenwindOpwek2023_kWh=ZuidenwindOpwek(2+35040:1+35040*2,3);
ZuidenwindOpwek2021_kWh=ZuidenwindOpwek(2+35040*2:end,3);

sum(ZuidenwindOpwek2021_kWh)/2352
sum(ZuidenwindOpwek2022_kWh)/2352
sum(ZuidenwindOpwek2023_kWh)/2352
