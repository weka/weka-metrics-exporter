#!/bin/bash

if [ $# -eq 0 ]
  then
    echo "No arguments supplied"
    exit
fi


ADDRESS=$@

curl -s http://$ADDRESS:14000/dist/v1/install | timeout 10 sh 1> /dev/null 2>/dev/null
cd /root/ && ./weka-metrics-exporter.py -a -H $@

