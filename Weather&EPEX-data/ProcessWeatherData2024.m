% Process weather data 2023 de Bilt
clear, clc;
% profiles=csvread("db_profiles.csv")(2:end,:);

t=0:8759;
dt=1;

startrow=24;

Weersdata = readmatrix("Weersdata2024deBilt.txt", ...
    "Delimiter", ",", ...
    "CommentStyle", "#");   % sla alle regels die met # beginnen over

ZonInstraling_kWpm2 = Weersdata(startrow+t,6)*100*100/3600/1000;
max(ZonInstraling_kWpm2);
WindsnelheidH10_mps=Weersdata(startrow+t,4)*0.1;
ruwheidshoogte_m=0.05;
WindsnelheidH100_mps=WindsnelheidH10_mps*log(100/ruwheidshoogte_m)/log(10/ruwheidshoogte_m);
Temperatuur_degC=Weersdata(startrow+t,5)*0.1;

% %load WindTurbinePower.mat;
% WindturbineMatData = readmatrix("WindturbinePower.txt", ...
%     "Delimiter", ",", ...
%     "CommentStyle", "#");
% turbinepower_kW = WindturbineMatData(1:length(WindturbineMatData)/2,1);
% windspeed_mps = WindturbineMatData(length(WindturbineMatData)/2+1:length(WindturbineMatData),1);
% 
% turbinepower_r = [turbinepower_kW]/max(turbinepower_kW);
% windspeed_mps = [windspeed_mps];
% 
% windPower_r = interp1(windspeed_mps, turbinepower_r, min(WindsnelheidH100_mps,max(windspeed_mps)));
% sum(windPower_r)

solar_e_prod_normalized = ZonInstraling_kWpm2/max(ZonInstraling_kWpm2) * 0.8;
sum(solar_e_prod_normalized)

return

%%

csvarray = [t', solar_e_prod_normalized, Temperatuur_degC]; %windPower_r
%csvwrite("WeatherEpexJan23Okt24.csv",csvarray)
csvwrite("db_profiles2024.csv",csvarray)

return
