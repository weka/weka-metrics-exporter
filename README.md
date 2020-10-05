# Weka Metrics Exporter

Metrics exporter for WekaFS. Gathers metrics and statistics from a Weka Cluster and exposes a metrics endpoint for Prometheus to scrape.

## Installation

1. Unpack tarball or clone repository.
2. Make sure you have Python 3.5+ and `pip` installed on you machine.
3. Install package dependencies: `pip install -r requirements.txt`.

## New in v2.0
The host specified with the -H parameter may now be a comma-separated list of hosts.  ie: weka1,weka2,weka3   

This will prevent the exporter from failing to start up if the host is down, similar to the way stateless client mounts work.

Fixed an error where old stats that had 0 values were still being reported with their last known value by prometheus

Performance improvements

## Metrics Exported

1. Edit the weka-metrics-exporter.yml file. All the metrics that can be collected are defined there. Note that the default contents are sufficient to populate the example dashboards. **Add ONLY what you need**. It does generate load on the cluster to collects these stats.
2. Uncomment any metrics you would like to gather that are commented out. Restart the exporter to start gathering those metrics.
3. To display new metrics in Grafana, add new panel(s) and query. Try to curl the data from the metrics endpoint.

## Usage

Configure prometheus to pull data from the Weka metrics exporter (see prometheus docs). Example Prometheus configuration:

```
# Weka
- job_name: 'weka'
  scrape_interval: 60s
  static_configs:
    - targets: ['localhost:8001']
```

*Important*: Currently the exporter pulls stats every 60 seconds so there is no use of setting the interval to a lower value.

It is recommended to be run as a daemon: `nohup ./weka-metrics-exporter.py -a -H <target host> &`. or better, run as a system service.

## Configuration

- Use the `-a` option to load-balance the data collection over all the Weka backends.
- Use `-H` hostname as needed (for example, if you're not running this on a client or backend).
- `-p` port sets the port number - Default port is 8001
- `-h` will print the help message.

## Docker build instructions (optional)

To build this repositiory:

```
git clone <this repo>
docker build --tag weka-metrics-exporter .
```
or
```docker build --tag weka-metrics-exporter https://github.com/weka/weka-metrics-exporter.git```

To save and/or load the image
```
docker save -o weka-metrics-exporter.tar weka-metrics-exporter
docker load -i  weka-metrics-exporter.tar
```

To run the image:  (one of the below examples)
```
docker run -d --network=host weka-metrics-exporter 172.20.40.1
# or
docker run -d -p 8001:8001 weka-metrics-exporter 172.20.40.1

# map /etc/hosts and a custom configuration .yml file into the container...
docker run -d -p 8001:8001 --mount type=bind,source=/etc/hosts,target=/etc/hosts --mount type=bind,source=$PWD/weka-metrics-exporter.yml,target=/root/weka-metrics-exporter.yml weka-metrics-exporter 172.20.40.1
```

Comments, issues, etc can be reported in the repository's issues.

Note: This is provided via the GPLv3 license agreement (text in LICENSE.md).  Using/copying this code implies you accept the agreement.

Maintained by vince@weka.io
