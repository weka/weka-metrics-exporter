#!/bin/bash

if [ $# -eq 0 ]
  then
    echo "No arguments supplied"
    exit
fi

cd /root/ && ./weka-metrics-exporter $@

