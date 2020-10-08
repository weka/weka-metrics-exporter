#!/bin/bash

if [ $# -eq 0 ]
  then
    echo "No arguments supplied"
    exit
fi

# Handle case where we have a comma-separated list of weka servers
IFS=',' read -r -a ADDRESSES <<< "$@"

for ADDRESS in "${ADDRESSES[@]}"
do
    # try to install the weka agent (weka command) from the server(s).  First one to install wins
    timeout 10.0 curl -s http://$ADDRESS:14000/dist/v1/install | sh &> /dev/null
    if [ $? == 0 ]; then
        # success! move on
        break
    fi
done

cd /root/ && ./weka-metrics-exporter -a -H $@

