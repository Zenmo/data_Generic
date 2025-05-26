% Loading epex prices from json file
epextxt = fileread("EpexJan23-Okt24.txt");
epexstruct = jsondecode(epextxt);
epex = [];

for i=1:length(epexstruct.Prices)
  epex(i) = epexstruct.Prices(i).price;
end
epex =[epex]
save epexJan23-Okt24.mat epex
figure;
plot(epex);
