# sns-weka-prometheus
Weka Prometheus Client v1.2.2

Prometheus Client for WekaFS.  Gathers statistics from a Weka Cluster and publishes so Prometheus can scrape it.

Installation:

    1. unpack tarball (I guess you've done that already! ;) )
    2. pip install prometheus_client (you might have to yum install python2-pip first)
    3. pip install pyyaml

if you don't have prometheus and grafana already:
    4. Configure prometheus to pull data from the weka prometheus client (see prometheus docs) 
          example snippet from prometheus.yml:

          # weka
          - job_name: 'weka'
            scrape_interval:     60s
            static_configs:
            - targets: ['localhost:8000']

    5. Configure Grafana to pull data from prometheus (see grafana docs)

    6. Import the included dashboards (.json files) into grafana
    7. run weka_prom_client.py
        Typical command-line:   nohup ./weka_prom_client.py -a -H <target host> &

        get help with:
        ./weka_prom_client.py -h

OPTIONAL:
    1. Edit the weka_prom.yaml file.   All the metrics that can be collected are defined there.  
        - Note that the contents of the .yaml are sufficient to populate the example dashboards
        - add ONLY what you need.  It does generate load on the cluster to collects these stats
    2. Uncomment any metrics you would like to gather that are commented out.  Restart the weka_prom_client.py
        to start gathering those metrics.
    3. To display new metrics in Grafana, add new panel(s) and query.   Try to curl the data from the weka_prom_client.py
        - to view what they look like: curl http://localhost:8000 (or whatever you set it to)


suggestions:
    1. use the -a option to load-balance the data collection over all the weka backends
    2. use -H hostname as needed (for example, if you're not running this on a client or backend)
    3. -p port sets the port number - Default port is 8000
        - ie: http://localhost:8000
    4. You can test with it in the foreground, but "nohup ./weka_prometheus_client.py -a &" is suggested, or
        better yet, set it up as a service
            - Someday we'll include the scripts for the service

Comments, issues, etc to vince@weka.io

