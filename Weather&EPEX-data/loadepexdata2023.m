% Loading epex prices from json file
epex2023txt = fileread("Epex2023.txt");
epex2023struct = jsondecode(epex2023txt);
epex2023 = [];

for i=1:8759
  epex2023(i) = epex2023struct.Prices(i).price;
end
epex2023=[epex2023 0]
save epex2023.mat epex2023
figure;
plot(epex2023);

% Loading epex prices from json file
epex2024txt = fileread("Epex2024.txt");
epex2024struct = jsondecode(epex2024txt);
epex2024 = [];

for i=1:8759
  epex2024(i) = epex2024struct.Prices(i).price;
end
epex2024=[epex2024 0]
save epex2024.mat epex2024
figure;
plot((1:8760)/24,epex2024);
